"""Adapt native acceptance history and preserve raw inbound frames."""

from __future__ import annotations

import typing as typ

import falcon
import falcon.asgi
import msgspec.json as msjson

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from ._session import SessionState
    from .protocols import WebSocketLike


_MIN_CLOSE_CODE = 1000
_RESERVED_CLOSE_CODE_RANGES = ((1004, 1006), (1015, 1999))


class SessionWebSocket:
    """Forward native Falcon operations while retaining session acceptance."""

    def __init__(self, websocket: falcon.asgi.WebSocket, session: SessionState) -> None:
        self._websocket = websocket
        self._session = session
        # Falcon's unaccepted becomes False after a rejected handshake closes.
        # Record acceptance while the socket is open, before that history is lost.
        self._accepted = not websocket.unaccepted and not websocket.closed
        self._closing = False
        if self._accepted:
            session.establish()

    @property
    def unaccepted(self) -> bool:
        """Whether the handshake has ever been accepted."""
        return not self._accepted

    @property
    def closed(self) -> bool:
        """Block new receives as soon as a close operation begins."""
        return self._closing or self._websocket.closed

    def __getattr__(self, name: str) -> object:
        """Preserve Falcon's additional public socket methods and attributes."""
        return getattr(self._websocket, name)

    async def accept(
        self,
        subprotocol: str | None = None,
        headers: cabc.Mapping[str, str] | cabc.Iterable[tuple[str, str]] | None = None,
    ) -> None:
        """Establish cleanup when native acceptance completes or then fails."""
        was_closed = self.closed
        try:
            await self._websocket.accept(subprotocol=subprotocol, headers=headers)
        finally:
            if not was_closed and not self._websocket.unaccepted:
                self._accepted = True
                self._session.establish()

    async def close(self, code: int | None = 1000, reason: str | None = None) -> None:
        """Start the receive guard before suspending in Falcon's close send."""
        # Falcon stops its receive pump before validating the code. Validate
        # first so a rejected argument cannot disable an accepted connection.
        _validate_close_code(code)
        self._closing = True
        await self._websocket.close(code=code, reason=reason)

    async def receive_raw(self) -> str | bytes:
        """Preserve mixed frame types while retaining Falcon's disconnect state."""
        if not self._websocket.ready:
            # Let the public API raise Falcon's stored disconnect code or its
            # pre-accept error before accessing the low-level receiver.
            await self._websocket.receive_media()
        # Falcon's public receivers either decode media or consume only one
        # frame type. Isolate this seam so Pachinko can dispatch both raw types
        # without changing application media handlers or losing disconnect codes.
        event = await self._websocket._receive()  # pyright: ignore[reportPrivateUsage]
        text = event.get("text")
        if isinstance(text, str):
            return text
        data = event.get("bytes")
        if isinstance(data, bytes):
            return data
        msg = "Missing TEXT or BINARY WebSocket payload"
        raise falcon.PayloadTypeError(msg)


async def receive_frame(ws: WebSocketLike) -> str | bytes:
    """Keep native frames raw and adapt injected media-only transports."""
    if isinstance(ws, SessionWebSocket):
        return await ws.receive_raw()
    media = await ws.receive_media()
    return media if isinstance(media, str | bytes) else msjson.encode(media)


def _validate_close_code(code: object) -> None:
    """Apply Falcon's code validation before its receive pump is stopped."""
    if code is None:
        return
    if not isinstance(code, int):
        msg = "code must be an int"
        # Preserve the native exception type for callers handling Falcon errors.
        raise ValueError(msg)  # ruff: ignore[type-check-without-type-error]
    if code < _MIN_CLOSE_CODE:
        msg = "Invalid close code. The value must be >= 1000"
        raise ValueError(msg)
    if any(lower <= code <= upper for lower, upper in _RESERVED_CLOSE_CODE_RANGES):
        msg = "Invalid close code. Only unreserved codes may be used."
        raise ValueError(msg)
