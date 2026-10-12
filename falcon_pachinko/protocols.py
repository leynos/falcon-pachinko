"""Protocol definitions used across the package."""

from __future__ import annotations

import typing as typ

if typ.TYPE_CHECKING:
    import collections.abc as cabc


class WebSocketLike(typ.Protocol):
    """Minimal interface for WebSocket connections."""

    async def accept(
        self,
        subprotocol: str | None = None,
        headers: cabc.Mapping[str, str] | cabc.Iterable[tuple[str, str]] | None = None,
    ) -> None:
        """Accept the WebSocket handshake."""

    async def close(self, code: int | None = 1000, reason: str | None = None) -> None:
        """Close the WebSocket connection."""

    async def send_media(self, media: object, /) -> None:
        """Send structured data over the connection."""

    async def send_text(self, message: str) -> None:
        """Send one unencoded text frame over the connection."""

    async def receive_media(self) -> object:
        """Receive structured data from the connection."""

    async def receive_text(self) -> str:
        """Receive one text frame from an accepted connection."""
