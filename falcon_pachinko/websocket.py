from __future__ import annotations

from types import MethodType
from typing import Any


class WebSocketConnectionManager:
    """Track active WebSocket connections."""

    def __init__(self) -> None:
        self.connections: dict[str, Any] = {}
        self.rooms: dict[str, set[str]] = {}


# Public API ---------------------------------------------------------------


def install(app: Any) -> None:
    """Attach WebSocket utilities to a Falcon app."""
    app.ws_connection_manager = WebSocketConnectionManager()
    app._websocket_routes = {}
    app.add_websocket_route = MethodType(_add_websocket_route, app)


def _add_websocket_route(self: Any, path: str, resource: Any) -> None:
    """Register a WebSocket resource for the given path."""
    self._websocket_routes[path] = resource
