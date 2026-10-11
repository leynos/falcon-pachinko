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
        self.connection_ids: list[str] = []
        self.disconnect_records: list[tuple[str, int]] = []
        self.disconnect_events: dict[str, asyncio.Event] = {}
        self.handler_errors: list[Exception] = []
        self.received_frames: list[str | bytes] = []
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


class RecordingResource(WebSocketResource):
    """Exercise echo, close, failure, and blocked-handler session outcomes."""

    def __init__(
        self,
        *,
        manager: WebSocketConnectionManager,
        recorder: DisconnectRecorder,
        close_on_connect: bool = False,
    ) -> None:
        self._manager = manager
        self._recorder = recorder
        self._conn_id: str | None = None
        self._close_on_connect = close_on_connect

    async def on_connect(
        self,
        req: falcon.Request,
        ws: WebSocketLike,
        **params: object,
    ) -> bool:
        """Register the socket; the router performs acceptance afterward."""
        del req, params
        conn_id = self._recorder.register()
        await self._manager.add_connection(conn_id, ws)
        self._conn_id = conn_id
        if self._close_on_connect:
            await ws.accept()
            await ws.close(4003)
            return False
        return True

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
        if conn_id is None:
            return
        await self._manager.remove_connection(conn_id)
        self._conn_id = None
        self._recorder.disconnect_records.append((conn_id, close_code))
        self._recorder.disconnect_events[conn_id].set()


class RejectingResource(WebSocketResource):
    """Reject before acceptance so no session cleanup can be promised."""

    def __init__(
        self, *, recorder: DisconnectRecorder, close_on_connect: bool = False
    ) -> None:
        self._recorder = recorder
        self._close_on_connect = close_on_connect

    async def on_connect(
        self,
        req: falcon.Request,
        ws: WebSocketLike,
        **params: object,
    ) -> bool:
        del req, params
        if self._close_on_connect:
            await ws.close()
        return False

    async def on_disconnect(self, ws: WebSocketLike, close_code: int) -> None:
        del ws, close_code
        self._recorder.rejected_disconnects += 1


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
    router = WebSocketRouter()
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
        close_on_connect=True,
    )
    prefix = "/ws"
    router.mount(prefix)
    app.add_route(prefix, router)
    app.add_route(prefix + "/{pachinko_path:path}", router)
    return LiveApplication(app, router, manager, recorder)


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
        config = uvicorn.Config(
            application.app,
            host="127.0.0.1",
            port=0,
            ws="websockets",
            lifespan="off",
            timeout_graceful_shutdown=1,
            access_log=False,
            log_level="critical",
        )
        server = server_factory(config)
        loop_errors: list[dict[str, object]] = []
        previous_handler = typ.cast(
            "_LoopExceptionHandler | None", loop.get_exception_handler()
        )

        def record_loop_error(
            _loop: asyncio.AbstractEventLoop, context: dict[str, typ.Any]
        ) -> None:
            loop_errors.append(context)

        loop.set_exception_handler(record_loop_error)
        serve_task = asyncio.create_task(server.serve())

        async def wait_until_started() -> None:
            while not server.started:
                if serve_task.done():
                    serve_task.result()
                await asyncio.sleep(0.01)

        try:
            if wait_ready is None:
                await asyncio.wait_for(wait_until_started(), timeout=5)
            else:
                await wait_ready(wait_until_started())
        except (Exception, asyncio.CancelledError):
            serve_task.cancel()
            try:
                await asyncio.gather(serve_task, return_exceptions=True)
            finally:
                loop.set_exception_handler(previous_handler)
            raise

        sockets = server.servers
        if not sockets or not sockets[0].sockets:
            loop.set_exception_handler(previous_handler)
            serve_task.cancel()
            with suppress(asyncio.CancelledError):
                await serve_task
            msg = "Uvicorn reported started without a listening socket"
            raise RuntimeError(msg)
        port = sockets[0].sockets[0].getsockname()[1]
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
