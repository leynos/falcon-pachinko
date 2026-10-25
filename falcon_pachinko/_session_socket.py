"""Adapt native acceptance history and preserve raw inbound frames."""

from __future__ import annotations

import asyncio
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


def _accept_arguments(
    subprotocol: str | None,
    headers: cabc.Mapping[str, str] | cabc.Iterable[tuple[str, str]] | None,
) -> dict[str, object]:
    """Build only the handshake keywords supplied by the caller."""
    kwargs: dict[str, object] = {}
    if subprotocol is not None:
        kwargs["subprotocol"] = subprotocol
    if headers is not None:
        kwargs["headers"] = headers
    return kwargs


class SessionWebSocket:
    """Forward socket operations while retaining observed session acceptance."""

    def __init__(
        self,
        websocket: WebSocketLike,
        native_websocket: falcon.asgi.WebSocket,
        session: SessionState,
    ) -> None:
        self._websocket = websocket
        self._native_websocket = native_websocket
        self._session = session
        # Falcon's unaccepted becomes False after a rejected handshake closes.
        # Record acceptance while the socket is open, before that history is lost.
        self._accepted = not native_websocket.unaccepted and not native_websocket.closed
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
        return self._closing or bool(getattr(self._websocket, "closed", False))

    def __getattr__(self, name: str) -> object:
        """Preserve the transport's additional public methods and attributes."""
        return getattr(self._websocket, name)

    async def accept(
        self,
        subprotocol: str | None = None,
        headers: cabc.Mapping[str, str] | cabc.Iterable[tuple[str, str]] | None = None,
    ) -> None:
        """Mirror Falcon's accepted state into shared session cleanup."""
        kwargs = _accept_arguments(subprotocol, headers)
        # Extra native keywords are outside WebSocketLike. Forward only supplied
        # options, letting each transport validate its own supported arguments.
        accept = typ.cast(
            "cabc.Callable[..., cabc.Awaitable[None]]", self._websocket.accept
        )
        was_closed = self.closed
        try:
            await accept(**kwargs)
        finally:
            if not was_closed and not self._native_websocket.unaccepted:
                self._accepted = True
                self._session.establish()

    async def close(self, code: int | None = 1000, reason: str | None = None) -> None:
        """Start the receive guard before suspending in the transport's close send."""
        # Falcon stops its receive pump before validating the code. Validate
        # first so a rejected argument cannot disable an accepted connection.
        _validate_close_code(code)
        kwargs: dict[str, object] = {"code": code}
        if reason is not None:
            kwargs["reason"] = reason
        close = typ.cast(
            "cabc.Callable[..., cabc.Awaitable[None]]", self._websocket.close
        )
        self._closing = True
        try:
            await close(**kwargs)
        except (Exception, asyncio.CancelledError):
            if not bool(getattr(self._websocket, "closed", False)):
                self._closing = False
            raise

    async def receive_payload(self) -> object:
        """Preserve raw Falcon text and binary frames for router dispatch."""
        websocket = self._native_websocket
        if not websocket.ready:
            # Let the public API raise Falcon's stored disconnect code or its
            # pre-accept error before accessing the low-level receiver.
            await websocket.receive_media()
        # Falcon's public receivers either decode media or consume only one
        # frame type. Isolate this seam so Pachinko can dispatch both raw types
        # without changing application media handlers or losing disconnect codes.
        event = await websocket._receive()  # pyright: ignore[reportPrivateUsage] - no public raw receiver
        text = event.get("text")
        if isinstance(text, str):
            return text
        data = event.get("bytes")
        if isinstance(data, bytes):
            return data
        msg = "Missing TEXT or BINARY WebSocket payload"
        raise falcon.PayloadTypeError(msg)

    async def receive_media(self) -> object:
        """Preserve Falcon's native media decoding for application callers."""
        return await self._websocket.receive_media()

    async def receive_text(self) -> str:
        """Receive a native or simulated frame and require text data."""
        payload = await self.receive_payload()
        if isinstance(payload, str):
            return payload
        msg = "expected a text WebSocket frame"
        raise TypeError(msg)


def track_session_socket(ws: WebSocketLike, session: SessionState) -> WebSocketLike:
    """Record acceptance when native state loses history or no probe exists."""
    native_websocket = _native_websocket(ws)
    if isinstance(native_websocket, falcon.asgi.WebSocket):
        # Delegated methods preserve the protocol; structural typing cannot
        # recover their signatures through __getattr__.
        return typ.cast(
            "WebSocketLike", SessionWebSocket(ws, native_websocket, session)
        )
    return ws


def _native_websocket(
    ws: WebSocketLike | falcon.asgi.WebSocket,
) -> WebSocketLike | falcon.asgi.WebSocket:
    """Resolve a live-server delegate's native Falcon socket when available."""
    candidate = getattr(ws, "_pachinko_native_websocket", ws)
    if isinstance(candidate, falcon.asgi.WebSocket):
        return candidate
    return typ.cast("WebSocketLike", ws)


async def receive_frame(ws: WebSocketLike) -> str | bytes:
    """Keep native frames raw and adapt injected media-only transports."""
    receive_payload = getattr(ws, "_receive_payload", None)
    media = (
        await receive_payload()
        if callable(receive_payload)
        else await ws.receive_media()
    )
    return media if isinstance(media, str | bytes) else msjson.encode(media)


def _validate_close_code(code: object) -> None:
    """Apply Falcon's code validation before its receive pump is stopped."""
    if code is None:
        return
    if not isinstance(code, int):
        msg = "code must be an int"
        # Preserve the native exception type for callers handling Falcon errors.
        raise ValueError(msg)  # ruff: ignore[type-check-without-type-error] - preserve Falcon ValueError
    if code < _MIN_CLOSE_CODE:
        msg = "Invalid close code. The value must be >= 1000"
        raise ValueError(msg)
    if any(lower <= code <= upper for lower, upper in _RESERVED_CLOSE_CODE_RANGES):
        msg = "Invalid close code. Only unreserved codes may be used."
        raise ValueError(msg)
