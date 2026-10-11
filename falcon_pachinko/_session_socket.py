"""Track acceptance history without depending on Falcon's private state."""

from __future__ import annotations

import typing as typ

import falcon
import falcon.asgi

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from ._session import SessionState


class _SessionWebSocket:
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
        self._closing = True
        await self._websocket.close(code=code, reason=reason)

    async def send_media(
        self,
        data: object,
        payload_type: falcon.WebSocketPayloadType = falcon.WebSocketPayloadType.TEXT,
    ) -> None:
        """Forward media and Falcon's optional payload type."""
        await self._websocket.send_media(data, payload_type=payload_type)

    async def receive_media(self) -> object:
        """Receive media through Falcon's configured media handlers."""
        return await self._websocket.receive_media()
