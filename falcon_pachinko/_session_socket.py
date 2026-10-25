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
    """Forward socket operations while retaining observed session acceptance."""

    def __init__(
        self, websocket: WebSocketLike | falcon.asgi.WebSocket, session: SessionState
    ) -> None:
        self._websocket = websocket
        self._session = session
        # Falcon's unaccepted becomes False after a rejected handshake closes.
        # Record acceptance while the socket is open, before that history is lost.
        self._accepted = (
            not websocket.unaccepted and not websocket.closed
            if isinstance(websocket, falcon.asgi.WebSocket)
            else False
        )
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

    @property
    def is_closed(self) -> bool:
        """Guard receive dispatch while the transport close is still pending."""
        return self.closed

    def __getattr__(self, name: str) -> object:
        """Preserve the transport's additional public methods and attributes."""
        return getattr(self._websocket, name)

    async def accept(
        self,
        subprotocol: str | None = None,
        headers: cabc.Mapping[str, str] | cabc.Iterable[tuple[str, str]] | None = None,
    ) -> None:
        """Establish cleanup from observed state or a completed acceptance call."""
        kwargs: dict[str, object] = {}
        if subprotocol is not None:
            kwargs["subprotocol"] = subprotocol
        if headers is not None:
            kwargs["headers"] = headers
        # Extra native keywords are outside WebSocketLike. Forward only supplied
        # options, letting each transport validate its own supported arguments.
        accept = typ.cast(
            "cabc.Callable[..., cabc.Awaitable[None]]", self._websocket.accept
        )
        was_closed = self.closed
        succeeded = False
        try:
            await accept(**kwargs)
            succeeded = True
        finally:
            # Missing state is initially unknown. Only a completed accept call
            # provides acceptance evidence when the transport has no probe.
            unaccepted = getattr(self._websocket, "unaccepted", not succeeded)
            if not was_closed and unaccepted is False:
                self._accepted = True
                self._session.establish()

    async def close(self, code: int | None = 1000, reason: str | None = None) -> None:
        """Start the receive guard before suspending in the transport's close send."""
        is_native = isinstance(self._websocket, falcon.asgi.WebSocket)
        # Falcon stops its receive pump before validating the code. Validate
        # first so a rejected argument cannot disable an accepted connection.
        if is_native:
            _validate_close_code(code)
        kwargs: dict[str, object] = {
            "code": 1000 if code is None and not is_native else code
        }
        if reason is not None:
            kwargs["reason"] = reason
        close = typ.cast(
            "cabc.Callable[..., cabc.Awaitable[None]]", self._websocket.close
        )
        was_closing = self._closing
        self._closing = True
        try:
            await close(**kwargs)
            self._record_close_code(code)
        except (TypeError, ValueError):
            if not is_native:
                self._closing = was_closing or bool(
                    getattr(self._websocket, "closed", False)
                )
            raise

    def _record_close_code(self, requested_code: int | None) -> None:
        """Retain the transport's effective close code after a successful send."""
        if self._session.close_code is not None:
            return
        fallback = 1000 if requested_code is None else requested_code
        reported_code = getattr(self._websocket, "close_code", fallback)
        if isinstance(reported_code, int):
            self._session.close_code = reported_code

    async def receive_payload(self) -> object:
        """Preserve native frame types and support injected media transports."""
        websocket = self._websocket
        if not isinstance(websocket, falcon.asgi.WebSocket):
            return await websocket.receive_media()
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


def track_session_socket(ws: WebSocketLike, session: SessionState) -> WebSocketLike:
    """Record acceptance when native state loses history or no probe exists."""
    if isinstance(ws, falcon.asgi.WebSocket) or not hasattr(ws, "unaccepted"):
        # Delegated methods preserve the protocol; structural typing cannot
        # recover their signatures through __getattr__.
        return typ.cast("WebSocketLike", SessionWebSocket(ws, session))
    return ws


async def receive_frame(ws: WebSocketLike) -> str | bytes:
    """Keep native frames raw and adapt injected media-only transports."""
    media = (
        await ws.receive_payload()
        if isinstance(ws, SessionWebSocket)
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
    if _is_reserved_close_code(code):
        msg = "Invalid close code. Only unreserved codes may be used."
        raise ValueError(msg)


def _is_reserved_close_code(code: int) -> bool:
    """Return whether a code falls inside a protocol-reserved range."""
    return any(lower <= code <= upper for lower, upper in _RESERVED_CLOSE_CODE_RANGES)
