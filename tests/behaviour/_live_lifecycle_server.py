"""Real Uvicorn harness for production-router WebSocket lifecycle scenarios."""

from __future__ import annotations

import asyncio
import collections.abc as cabc
import dataclasses as dc
import itertools
import typing as typ
from contextlib import suppress

import falcon.asgi
import msgspec as ms
import msgspec.json as msjson
import uvicorn

from falcon_pachinko import (
    HookCollection,
    HookContext,
    WebSocketConnectionManager,
    WebSocketResource,
    WebSocketRouter,
)

if typ.TYPE_CHECKING:
    from falcon_pachinko.protocols import WebSocketLike


type _LoopExceptionHandler = cabc.Callable[
    [asyncio.AbstractEventLoop, dict[str, typ.Any]], None
]


class DisconnectRecorder:
    """Record accepted connections, failures, and completed disconnects."""

    def __init__(self) -> None:
        """Create independent observations and readiness events for one live app."""
        self.connection_ids: list[str] = []
        self.disconnect_records: list[tuple[str, int]] = []
        self.disconnect_events: dict[str, asyncio.Event] = {}
        self.handler_errors: list[Exception] = []
        self.received_frames: list[str | bytes] = []
        self.decoded_media: list[object] = []
        self.events: list[str] = []
        self.accept_calls = 0
        self.before_disconnect_errors: list[BaseException | None] = []
        self.connection_ready = asyncio.Event()
        self.message_processed = asyncio.Event()
        self.rejected_disconnects = 0
        self.block_started = asyncio.Event()
        self._ids = itertools.count(1)

    def register(self) -> str:
        """Create a stable identifier and event for the next resource."""
        conn_id = f"live-{next(self._ids)}"
        self.connection_ids.append(conn_id)
        self.disconnect_events[conn_id] = asyncio.Event()
        return conn_id

    async def wait_for_disconnect(self, conn_id: str) -> None:
        """Wait for one resource to finish its disconnect hook."""
        await asyncio.wait_for(self.disconnect_events[conn_id].wait(), timeout=5)


class AcceptanceCountingWebSocket:
    """Delegate to Falcon while counting each real handshake acceptance."""

    def __init__(self, websocket: WebSocketLike, recorder: DisconnectRecorder) -> None:
        """Retain Falcon's native socket while making real acceptance countable."""
        self._websocket = websocket
        self._pachinko_native_websocket = websocket
        self._recorder = recorder

    def __getattr__(self, name: str) -> object:
        """Forward the native Falcon WebSocket surface."""
        return getattr(self._websocket, name)

    async def accept(
        self,
        subprotocol: str | None = None,
        headers: cabc.Mapping[str, str] | cabc.Iterable[tuple[str, str]] | None = None,
    ) -> None:
        """Count and forward one real Falcon acceptance call."""
        self._recorder.accept_calls += 1
        self._recorder.events.append("accept")
        await self._websocket.accept(subprotocol=subprotocol, headers=headers)

    async def close(self, code: int | None = 1000, reason: str | None = None) -> None:
        await self._websocket.close(code, reason)

    async def send_media(self, media: object) -> None:
        await self._websocket.send_media(media)

    async def send_text(self, message: str) -> None:
        await self._websocket.send_text(message)

    async def receive_media(self) -> object:
        return await self._websocket.receive_media()

    async def receive_text(self) -> str:
        return await self._websocket.receive_text()


@dc.dataclass(frozen=True, slots=True)
class RecordingOptions:
    """Select one focused outcome for a live lifecycle scenario."""

    close_on_connect: bool = False
    fail_session_start: bool = False
    hello_exchange: bool = False
    legacy_accept: bool = False
    receive_decoded_media: bool = False


class RecordingResource(WebSocketResource):
    """Exercise echo, close, failure, and blocked-handler session outcomes."""

    def __init__(
        self,
        *,
        manager: WebSocketConnectionManager,
        recorder: DisconnectRecorder,
        options: RecordingOptions | None = None,
    ) -> None:
        """Bind the manager and recorder used to prove live session cleanup."""
        self._manager = manager
        self._recorder = recorder
        self._conn_id: str | None = None
        self._options = options if options is not None else RecordingOptions()

    async def on_connect(
        self,
        req: falcon.Request,
        ws: WebSocketLike,
        **params: object,
    ) -> bool:
        """Approve normal setup; retain one explicit deprecated-path probe."""
        del req, params
        if self._options.close_on_connect:
            await ws.accept()
            conn_id = self._recorder.register()
            await self._manager.add_connection(conn_id, ws)
            self._conn_id = conn_id
            self._recorder.connection_ready.set()
            await ws.close(4003)
            return False
        if self._options.legacy_accept:
            await ws.accept()
        return True

    async def on_session_start(self, ws: WebSocketLike) -> None:
        """Register active membership and optionally exercise hello auth."""
        conn_id = self._recorder.register()
        await self._manager.add_connection(conn_id, ws)
        self._conn_id = conn_id
        self._recorder.connection_ready.set()
        if self._options.fail_session_start:
            error = RuntimeError("live WebSocket session start failed")
            self._recorder.handler_errors.append(error)
            raise error
        if self._options.hello_exchange:
            self._recorder.events.append("session_start")
            await ws.send_text("server hello")
            async with asyncio.timeout(2):
                reply = await ws.receive_text()
            await ws.send_text(f"confirmed: {reply}")
        if self._options.receive_decoded_media:
            media = await ws.receive_media()
            self._recorder.decoded_media.append(media)
            await ws.send_media({"received": media})

    async def on_unhandled(self, ws: WebSocketLike, message: str | bytes) -> None:
        """Implement the small message protocol used by live scenarios."""
        self._recorder.received_frames.append(message)
        try:
            payload = msjson.decode(message)
        except ms.DecodeError:
            if not isinstance(message, str):
                raise
            await ws.send_media({"type": "raw", "message": message})
            return
        if not isinstance(payload, dict):
            msg = "live test messages must be JSON objects"
            raise TypeError(msg)

        # JSON object keys are strings; decoded values can be any JSON value.
        await self._respond_to_payload(ws, typ.cast("dict[str, object]", payload))

    async def _respond_to_payload(
        self, ws: WebSocketLike, payload: dict[str, object]
    ) -> None:
        """Exercise handler exits independently of raw frame decoding."""
        match payload.get("type"):
            case "echo":
                await ws.send_media(payload)
            case "close-me" | "close-and-send":
                await ws.close(4001)
                if payload.get("type") == "close-and-send":
                    await ws.send_media(payload)
            case "boom":
                error = RuntimeError("live WebSocket handler failed")
                self._recorder.handler_errors.append(error)
                raise error
            case "block":
                self._recorder.block_started.set()
                await asyncio.Event().wait()
            case _:
                pass

    async def on_disconnect(self, ws: WebSocketLike, close_code: int) -> None:
        """Remove the accepted socket and publish the final close code."""
        del ws
        conn_id = self._conn_id
        self._conn_id = None
        if conn_id is None:
            return
        await self._manager.remove_connection(conn_id)
        self._recorder.disconnect_records.append((conn_id, close_code))
        self._recorder.disconnect_events[conn_id].set()


class RejectingResource(WebSocketResource):
    """Reject before acceptance so no session cleanup can be promised."""

    def __init__(
        self, *, recorder: DisconnectRecorder, close_on_connect: bool = False
    ) -> None:
        """Select metadata rejection or explicit pre-accept closure for the test."""
        self._recorder = recorder
        self._close_on_connect = close_on_connect
        self._conn_id: str | None = None

    async def on_connect(
        self,
        req: falcon.Request,
        ws: WebSocketLike,
        **params: object,
    ) -> bool:
        del params
        if self._close_on_connect:
            await ws.close()
        return bool(req.get_header("x-session-token"))

    async def on_session_start(self, ws: WebSocketLike) -> None:
        """Record that request metadata admitted an active session."""
        del ws
        self._conn_id = self._recorder.register()
        self._recorder.connection_ready.set()

    async def on_disconnect(self, ws: WebSocketLike, close_code: int) -> None:
        del ws
        conn_id = self._conn_id
        if conn_id is None:
            self._recorder.rejected_disconnects += 1
            return
        self._recorder.disconnect_records.append((conn_id, close_code))
        self._recorder.disconnect_events[conn_id].set()


def _bind_hook_events(
    hooks: HookCollection, recorder: DisconnectRecorder, layer: str
) -> None:
    """Record lifecycle hook order for an app-specific resource instance."""
    for event in (
        "before_connect",
        "after_connect",
        "before_receive",
        "after_receive",
        "before_disconnect",
    ):
        hooks.add(
            event,
            lambda _context, name=event: recorder.events.append(f"{layer}.{name}"),
        )


class LifecycleChildResource(RecordingResource):
    """Nested resource whose callbacks expose the complete hook sequence."""

    def __init__(
        self,
        *,
        manager: WebSocketConnectionManager,
        recorder: DisconnectRecorder,
    ) -> None:
        """Attach a child hook layer to the live manager and recorder."""
        super().__init__(manager=manager, recorder=recorder)
        object.__setattr__(self, "hooks", HookCollection())
        _bind_hook_events(self.hooks, recorder, "child")

    async def on_connect(
        self,
        req: falcon.Request,
        ws: WebSocketLike,
        **params: object,
    ) -> bool:
        del req, ws, params
        self._recorder.events.append("child.on_connect")
        return True

    async def on_session_start(self, ws: WebSocketLike) -> None:
        self._recorder.events.append("child.on_session_start")
        await super().on_session_start(ws)

    async def on_unhandled(self, ws: WebSocketLike, message: str | bytes) -> None:
        self._recorder.events.append("child.dispatch")
        await super().on_unhandled(ws, message)
        self._recorder.message_processed.set()

    async def on_disconnect(self, ws: WebSocketLike, close_code: int) -> None:
        self._recorder.events.append("child.on_disconnect")
        await super().on_disconnect(ws, close_code)


class LifecycleParentResource(WebSocketResource):
    """Parent resource that records its layer around a nested child route."""

    def __init__(
        self, *, manager: WebSocketConnectionManager, recorder: DisconnectRecorder
    ) -> None:
        """Build the parent layer and inject shared context into its child."""
        self._manager = manager
        self._recorder = recorder
        self.add_subroute("child", LifecycleChildResource)
        object.__setattr__(self, "hooks", HookCollection())
        _bind_hook_events(self.hooks, recorder, "parent")

    def get_child_context(self) -> dict[str, object]:
        """Supply the same manager and recorder to the nested child resource."""
        return {"manager": self._manager, "recorder": self._recorder}


@dc.dataclass(slots=True)
class LiveApplication:
    """Application pieces shared by the live server and BDD assertions."""

    app: falcon.asgi.App[falcon.asgi.Request, falcon.asgi.Response]
    router: WebSocketRouter
    manager: WebSocketConnectionManager
    recorder: DisconnectRecorder


def create_live_application() -> LiveApplication:
    """Build Falcon with a router mounted on the exact and catch-all paths."""
    app: falcon.asgi.App[falcon.asgi.Request, falcon.asgi.Response] = falcon.asgi.App()
    manager = WebSocketConnectionManager()
    recorder = DisconnectRecorder()
    router = WebSocketRouter(
        simulator_factory=lambda _req, ws: AcceptanceCountingWebSocket(ws, recorder)
    )
    _add_live_routes(router, manager, recorder)
    _add_global_hooks(router, recorder)
    prefix = "/ws"
    router.mount(prefix)
    app.add_route(prefix, router)
    app.add_route(prefix + "/{pachinko_path:path}", router)
    return LiveApplication(app, router, manager, recorder)


def _add_live_routes(
    router: WebSocketRouter,
    manager: WebSocketConnectionManager,
    recorder: DisconnectRecorder,
) -> None:
    """Register the focused endpoints exercised by live lifecycle scenarios."""
    router.add_route(
        "/session",
        RecordingResource,
        manager=manager,
        recorder=recorder,
    )
    router.add_route("/reject", RejectingResource, recorder=recorder)
    router.add_route(
        "/reject-close", RejectingResource, recorder=recorder, close_on_connect=True
    )
    router.add_route(
        "/accept-close",
        RecordingResource,
        manager=manager,
        recorder=recorder,
        options=RecordingOptions(close_on_connect=True),
    )
    router.add_route(
        "/start-failure",
        RecordingResource,
        manager=manager,
        recorder=recorder,
        options=RecordingOptions(fail_session_start=True),
    )
    router.add_route(
        "/hello",
        RecordingResource,
        manager=manager,
        recorder=recorder,
        options=RecordingOptions(hello_exchange=True),
    )
    router.add_route(
        "/decoded-media",
        RecordingResource,
        manager=manager,
        recorder=recorder,
        options=RecordingOptions(receive_decoded_media=True),
    )
    router.add_route(
        "/deprecated-accept",
        RecordingResource,
        manager=manager,
        recorder=recorder,
        options=RecordingOptions(legacy_accept=True),
    )
    router.add_route(
        "/lifecycle",
        LifecycleParentResource,
        manager=manager,
        recorder=recorder,
    )


def _add_global_hooks(router: WebSocketRouter, recorder: DisconnectRecorder) -> None:
    """Record each global hook in the live lifecycle event stream."""
    for event in (
        "before_connect",
        "after_connect",
        "before_receive",
        "after_receive",
        "before_disconnect",
    ):
        if event == "before_disconnect":

            def record_disconnect(context: HookContext) -> None:
                """Capture the cleanup error while recording the global hook order."""
                recorder.events.append("global.before_disconnect")
                recorder.before_disconnect_errors.append(context.error)

            router.global_hooks.add(event, record_disconnect)
        else:
            router.global_hooks.add(
                event,
                lambda _context, name=event: recorder.events.append(f"global.{name}"),
            )


@dc.dataclass(slots=True)
class LiveServer:
    """Run Uvicorn on a caller-owned event loop and capture loop errors."""

    application: LiveApplication
    server: uvicorn.Server
    serve_task: asyncio.Task[None]
    loop: asyncio.AbstractEventLoop
    port: int
    loop_errors: list[dict[str, object]]
    _previous_exception_handler: _LoopExceptionHandler | None = dc.field(repr=False)
    _stopped: bool = False

    @classmethod
    async def start(
        cls,
        loop: asyncio.AbstractEventLoop,
        *,
        server_factory: cabc.Callable[
            [uvicorn.Config], uvicorn.Server
        ] = uvicorn.Server,
        wait_ready: cabc.Callable[
            [cabc.Coroutine[object, object, None]], cabc.Awaitable[None]
        ]
        | None = None,
    ) -> LiveServer:
        """Start one ephemeral Uvicorn listener and wait for readiness."""
        application = create_live_application()
        server = server_factory(_server_config(application))
        loop_errors: list[dict[str, object]] = []
        previous_handler = typ.cast(
            "_LoopExceptionHandler | None", loop.get_exception_handler()
        )
        loop.set_exception_handler(_exception_recorder(loop_errors))
        serve_task = asyncio.create_task(server.serve())
        try:
            await _wait_for_startup(server, serve_task, wait_ready)
            port = _listening_port(server)
        except (Exception, asyncio.CancelledError):
            await _cleanup_failed_start(loop, previous_handler, serve_task)
            raise
        return cls(
            application,
            server,
            serve_task,
            loop,
            port,
            loop_errors,
            previous_handler,
        )

    @property
    def base_url(self) -> str:
        """Insecure loopback URL used by the local test client."""
        return f"ws://127.0.0.1:{self.port}"

    async def stop(self) -> None:
        """Ask Uvicorn to shut down and join its serving task."""
        if self._stopped:
            return
        self._stopped = True
        self.server.should_exit = True
        try:
            await asyncio.wait_for(asyncio.shield(self.serve_task), timeout=5)
        except TimeoutError:
            self.serve_task.cancel()
            with suppress(asyncio.CancelledError):
                await self.serve_task
            raise
        finally:
            try:
                # Uvicorn's graceful timeout cancels ASGI tasks without joining
                # them. Keep the loop alive until their shielded cleanup ends.
                session_tasks = tuple(self.server.server_state.tasks)
                if session_tasks:
                    await asyncio.wait_for(
                        asyncio.gather(*session_tasks, return_exceptions=True),
                        timeout=5,
                    )
            finally:
                self.loop.set_exception_handler(self._previous_exception_handler)


def _server_config(application: LiveApplication) -> uvicorn.Config:
    """Build the loopback-only configuration used by live tests."""
    return uvicorn.Config(
        application.app,
        host="127.0.0.1",
        port=0,
        ws="websockets",
        lifespan="off",
        timeout_graceful_shutdown=1,
        access_log=False,
        log_level="critical",
    )


def _exception_recorder(
    loop_errors: list[dict[str, object]],
) -> _LoopExceptionHandler:
    """Return a handler that records unexpected loop errors during a test."""

    def record_loop_error(
        _loop: asyncio.AbstractEventLoop, context: dict[str, typ.Any]
    ) -> None:
        """Retain unexpected ASGI task failures for live test assertions."""
        loop_errors.append(context)

    return record_loop_error


async def _wait_for_startup(
    server: uvicorn.Server,
    serve_task: asyncio.Task[None],
    wait_ready: cabc.Callable[
        [cabc.Coroutine[object, object, None]], cabc.Awaitable[None]
    ]
    | None,
) -> None:
    """Wait for server readiness or an injected test synchronization point."""
    wait_until_started = _wait_until_started(server, serve_task)
    if wait_ready is None:
        await asyncio.wait_for(wait_until_started, timeout=5)
    else:
        await wait_ready(wait_until_started)


async def _wait_until_started(
    server: uvicorn.Server, serve_task: asyncio.Task[None]
) -> None:
    """Poll the ready flag while surfacing an early serving-task failure."""
    while not server.started:
        if serve_task.done():
            serve_task.result()
        await asyncio.sleep(0.01)


def _listening_port(server: uvicorn.Server) -> int:
    """Return the first bound port, or fail if startup exposed no listener."""
    sockets = server.servers
    if sockets and sockets[0].sockets:
        return typ.cast("int", sockets[0].sockets[0].getsockname()[1])
    msg = "Uvicorn reported started without a listening socket"
    raise RuntimeError(msg)


async def _cleanup_failed_start(
    loop: asyncio.AbstractEventLoop,
    previous_handler: _LoopExceptionHandler | None,
    serve_task: asyncio.Task[None],
) -> None:
    """Stop a failed start and restore the event loop's prior error handler."""
    serve_task.cancel()
    try:
        await asyncio.gather(serve_task, return_exceptions=True)
    finally:
        loop.set_exception_handler(previous_handler)
