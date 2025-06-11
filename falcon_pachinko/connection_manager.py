from __future__ import annotations

import json
from typing import Any


class WebSocketConnectionManager:
    """Manage active WebSocket connections."""

    __slots__ = ("_connections",)

    def __init__(self) -> None:
        self._connections: dict[str, Any] = {}

    async def add_connection(self, connection_id: str, websocket: Any) -> None:
        """Register a new connection."""
        self._connections[connection_id] = websocket

    async def remove_connection(self, connection_id: str) -> None:
        """Remove a connection."""
        self._connections.pop(connection_id, None)

    async def send_to_connection(self, connection_id: str, message: Any) -> None:
        """Send a message to a specific connection."""
        websocket = self._connections.get(connection_id)
        if websocket is not None:
            await websocket.send_text(
                message if isinstance(message, str) else json.dumps(message)
            )

    def get_connection_ids(self) -> list[str]:
        """Return the IDs of all active connections."""
        return list(self._connections.keys())
