"""Deterministic socket and resource fixtures for lifecycle contract tests."""

from __future__ import annotations

import asyncio
import collections as cl
import collections.abc as cabc
import types
import typing as typ

import falcon

from falcon_pachinko.resource import WebSocketResource
from falcon_pachinko.router import WebSocketRouter


class SessionSocket:
    """Small Falcon-shaped socket with deterministic receive outcomes."""

    def __init__(
        self,
        *frames: object,
        block_receive: bool = False,
    ) -> None:
        """Seed deterministic frames and events for blocked receive schedules."""
        self.unaccepted = True
        self.closed = False
        self.accepted = asyncio.Event()
        self.receive_started = asyncio.Event()
        self._frames: cl.deque[object] = cl.deque(frames)
        self._block_receive = block_receive
        self._release_receive = asyncio.Event()
        self.close_codes: list[int] = []

    async def accept(
        self,
        subprotocol: str | None = None,
        headers: cabc.Mapping[str, str] | cabc.Iterable[tuple[str, str]] | None = None,
    ) -> None:
        """Record acceptance and wake setup observers."""
        del subprotocol, headers
        self.unaccepted = False
        self.accepted.set()

    async def close(self, code: int | None = 1000, reason: str | None = None) -> None:
        """Keep the first close code for deterministic disconnects."""
        del reason
        if self.closed:
            return
        self.closed = True
        self.close_codes.append(1000 if code is None else code)

    @staticmethod
    async def send_media(media: object) -> None:
        """Permit protocol sends without recording irrelevant replies."""
        del media

    async def send_text(self, message: str) -> None:
        """Permit text sends through the guarded test transport."""
        await self.send_media(message)

    async def receive_media(self) -> object:
        """Yield seeded frames or the configured transport failure."""
        self.receive_started.set()
        if self._block_receive:
            await self._release_receive.wait()
        if self._frames:
            frame = self._frames.popleft()
            if isinstance(frame, BaseException):
                raise frame
            return frame
        raise falcon.WebSocketDisconnected(self.close_codes[0] if self.closed else 1000)

    async def receive_text(self) -> str:
        """Return one queued text frame or preserve the peer disconnect."""
        message = await self.receive_media()
        if not isinstance(message, str):
            msg = "expected a text WebSocket frame"
            raise TypeError(msg)
        return message


def make_session_request(path: str = "/session", path_template: str = "/") -> object:
    """Create request attributes read by the production router."""
    return types.SimpleNamespace(path=path, path_template=path_template)


class RecordingResource(WebSocketResource):
    """Capture lifecycle calls and optionally block or fail session work."""

    instances: typ.ClassVar[list[RecordingResource]] = []

    def __init__(self, *, accepted: bool = True) -> None:
        """Retain setup decisions and synchronization points for cleanup tests."""
        self.accept_on_connect = accepted
        self.disconnect_codes: list[int] = []
        self.disconnect_started = asyncio.Event()
        self.finish_disconnect = asyncio.Event()
        self.wait_in_disconnect = False
        self.raw_frames: list[str | bytes] = []
        self.handled_frames: list[str | bytes] = []
        self.dispatch_error: Exception | None = None
        type(self).instances.append(self)

    async def on_connect(
        self, req: falcon.Request, ws: object, **params: object
    ) -> bool:
        """Return the configured setup decision."""
        del req, ws, params
        return self.accept_on_connect

    async def on_disconnect(self, ws: object, close_code: int) -> None:
        """Record cleanup entry and optionally wait for a test to release it."""
        del ws
        self.disconnect_codes.append(close_code)
        self.disconnect_started.set()
        if self.wait_in_disconnect:
            await self.finish_disconnect.wait()

    async def dispatch(self, ws: object, raw: str | bytes) -> None:
        """Record raw dispatch before optionally raising a handler failure."""
        self.raw_frames.append(raw)
        if self.dispatch_error is not None:
            raise self.dispatch_error
        await super().dispatch(typ.cast("typ.Any", ws), raw)

    async def on_unhandled(self, ws: object, message: str | bytes) -> None:
        """Capture frames reaching the resource fallback."""
        del ws
        self.handled_frames.append(message)


def make_session_router(
    resource: type[WebSocketResource] = RecordingResource,
) -> WebSocketRouter:
    """Mount a fresh router and clear resource observations."""
    RecordingResource.instances.clear()
    router = WebSocketRouter()
    router.add_route("/session", resource)
    router.mount("/")
    return router
