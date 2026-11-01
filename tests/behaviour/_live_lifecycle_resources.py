"""Lifecycle resources and observations for real WebSocket scenarios."""

from __future__ import annotations

import asyncio
import dataclasses as dc
import itertools
import typing as typ

import msgspec as ms
import msgspec.json as msjson

from falcon_pachinko import HookCollection, WebSocketResource

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    import falcon

    from falcon_pachinko import WebSocketConnectionManager
    from falcon_pachinko.protocols import WebSocketLike


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
        """Forward closure to Falcon's native socket.

        Parameters
        ----------
        code : int or None
            Close code forwarded to Falcon.
        reason : str or None
            Close reason forwarded to Falcon.
        """
        await self._websocket.close(code, reason)

    async def send_media(self, media: object) -> None:
        """Forward a structured frame to Falcon's native socket.

        Parameters
        ----------
        media : object
            Frame payload forwarded to Falcon.
        """
        await self._websocket.send_media(media)

    async def send_text(self, message: str) -> None:
        """Forward a text frame to Falcon's native socket.

        Parameters
        ----------
        message : str
            Text payload forwarded to Falcon.
        """
        await self._websocket.send_text(message)

    async def receive_media(self) -> object:
        """Receive one frame from Falcon's native socket.

        Returns
        -------
        object
            The received frame payload.
        """
        return await self._websocket.receive_media()

    async def receive_text(self) -> str:
        """Receive one text frame from Falcon's native socket.

        Returns
        -------
        str
            The received text payload.
        """
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
        """Admit connections carrying the configured session metadata.

        Parameters
        ----------
        req : falcon.Request
            Incoming request inspected for the session token.
        ws : WebSocketLike
            Socket closed when the scenario requests pre-accept closure.
        **params : object
            Router path parameters, unused by this fixture.

        Returns
        -------
        bool
            Whether the request includes an ``x-session-token`` header.
        """
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
        """Record cleanup for admitted sessions or unexpected rejected cleanup.

        Parameters
        ----------
        ws : WebSocketLike
            Accepted socket, unused by this recorder.
        close_code : int
            Effective close code supplied to the cleanup callback.
        """
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
        """Accept the child connection and record its lifecycle event.

        Parameters
        ----------
        req : falcon.Request
            Incoming request, unused by this fixture.
        ws : WebSocketLike
            Accepted socket, unused during the decision.
        **params : object
            Captured nested-route parameters, unused by this fixture.

        Returns
        -------
        bool
            Always true for this child resource.
        """
        del req, ws, params
        self._recorder.events.append("child.on_connect")
        return True

    async def on_session_start(self, ws: WebSocketLike) -> None:
        """Record the child session start and register it with the parent.

        Parameters
        ----------
        ws : WebSocketLike
            Accepted socket passed to the parent lifecycle hook.
        """
        self._recorder.events.append("child.on_session_start")
        await super().on_session_start(ws)

    async def on_unhandled(self, ws: WebSocketLike, message: str | bytes) -> None:
        """Record and process an unhandled frame in the child resource.

        Parameters
        ----------
        ws : WebSocketLike
            Accepted socket passed to the parent handler.
        message : str or bytes
            Raw frame dispatched to the child.
        """
        self._recorder.events.append("child.dispatch")
        await super().on_unhandled(ws, message)
        self._recorder.message_processed.set()

    async def on_disconnect(self, ws: WebSocketLike, close_code: int) -> None:
        """Record child cleanup before delegating to the parent resource.

        Parameters
        ----------
        ws : WebSocketLike
            Accepted socket passed to the parent cleanup hook.
        close_code : int
            Effective close code passed to the parent cleanup hook.
        """
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
