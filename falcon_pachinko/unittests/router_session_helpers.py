"""Socket and resource doubles for router-owned session lifecycle tests."""

from __future__ import annotations

import asyncio
import collections
import typing as typ

import falcon

from falcon_pachinko import WebSocketLike, WebSocketResource, WebSocketRouter


class SessionSocket(WebSocketLike):
    """Yield real-session-shaped frames, or block until cancellation."""

    def __init__(
        self,
        frames: tuple[object, ...] | None = (),
        *,
        accept_error: Exception | None = None,
    ) -> None:
        self.accepted = False
        self.closed = False
        self.receive_calls = 0
        self.frames = None if frames is None else collections.deque(frames)
        self.accept_error = accept_error
        self.receiving = asyncio.Event()

    async def accept(self, subprotocol: str | None = None) -> None:
        """Accept unless the configured handshake failure is raised."""
        if self.accept_error is not None:
            raise self.accept_error
        self.accepted = True

    async def close(self, code: int = 1000) -> None:
        """Record session closure."""
        self.closed = True

    async def send_media(self, data: object) -> None:
        """Reject sends; these router tests record dispatch directly."""
        raise NotImplementedError

    async def receive_media(self) -> object:
        """Yield queued frames then a peer disconnect with code 4000."""
        self.receive_calls += 1
        self.receiving.set()
        if self.frames is None:
            await asyncio.Event().wait()
        if self.frames:
            return self.frames.popleft()
        raise falcon.WebSocketDisconnected(4000)


class SessionResource(WebSocketResource):
    """Record session dispatch and exactly-once disconnect cleanup."""

    def __init__(self, *, dispatch_error: Exception | None = None) -> None:
        self.frames: list[str | bytes] = []
        self.disconnects: list[tuple[object, int]] = []
        self.dispatch_error = dispatch_error

    @typ.override
    async def on_connect(self, req: object, ws: object, **params: object) -> bool:
        """Accept the connection."""
        return True

    @typ.override
    async def dispatch(self, ws: object, raw: str | bytes) -> None:
        """Record frames in order, unless dispatch is configured to fail."""
        self.frames.append(raw)
        if self.dispatch_error is not None:
            raise self.dispatch_error

    @typ.override
    async def on_disconnect(self, ws: object, close_code: int) -> None:
        """Record each cleanup invocation and observed close code."""
        self.disconnects.append((ws, close_code))


def session_router(resource: SessionResource, *, receive_loop: bool) -> WebSocketRouter:
    """Mount a single resource for opt-in session lifecycle tests."""
    router = WebSocketRouter(receive_loop=receive_loop)
    router.add_route("/session", lambda: resource)
    router.mount("/")
    return router
