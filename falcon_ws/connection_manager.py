from __future__ import annotations

from dataclasses import dataclass, field
import json
import typing as t

import falcon.asgi


@dataclass(slots=True)
class WebSocketConnectionManager:
    """Generic manager for WebSocket connections.

    The base implementation only tracks connections and lets subclasses
    implement additional features such as pub/sub integration. This mirrors how
    Falcon resources are extended for HTTP routing.
    """

    _connections: dict[str, falcon.asgi.WebSocket] = field(
        default_factory=dict[str, falcon.asgi.WebSocket]
    )

    async def add_connection(
        self, connection_id: str, websocket: falcon.asgi.WebSocket
    ) -> None:
        """Register a new connection."""
        self._connections[connection_id] = websocket

    async def remove_connection(self, connection_id: str) -> None:
        """Remove a connection."""
        self._connections.pop(connection_id, None)

    async def send_to_connection(self, connection_id: str, message: t.Any) -> None:
        """Send a message to a specific connection."""
        websocket = self._connections.get(connection_id)
        if websocket is not None:
            await websocket.send_text(
                message if isinstance(message, str) else json.dumps(message)
            )
