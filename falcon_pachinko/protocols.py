"""Protocol definitions used across the package."""

from __future__ import annotations

import typing as typ


class WebSocketLike(typ.Protocol):
    """Minimal interface for WebSocket connections."""

    async def accept(self, subprotocol: str | None = None) -> None:
        """Accept the WebSocket handshake."""

    async def close(self, code: int = 1000) -> None:
        """Close the WebSocket connection."""

    async def send_media(self, data: object) -> None:
        """Send structured data over the connection."""
