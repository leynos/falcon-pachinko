from __future__ import annotations

from types import MethodType
from typing import Any


class WebSocketConnectionManager:
    """Track active WebSocket connections."""

    def __init__(self) -> None:
        """
        Initialises a WebSocketConnectionManager with empty connection and room mappings.
        
        Sets up internal dictionaries to track active WebSocket connections and organise them into rooms.
        """
        self.connections: dict[str, Any] = {}
        self.rooms: dict[str, set[str]] = {}


# Public API ---------------------------------------------------------------


def install(app: Any) -> None:
    """
    Attaches WebSocket connection management capabilities to a Falcon app instance.
    
    Initialises a WebSocketConnectionManager and associates it with the app if not already present. Also sets up internal structures for managing WebSocket routes and dynamically adds a method for registering WebSocket resource handlers.
    """
    if hasattr(app, "ws_connection_manager"):
        # Already installed – do nothing.
        return

    app.ws_connection_manager = WebSocketConnectionManager()
    app._websocket_routes = {}
    app.add_websocket_route = MethodType(_add_websocket_route, app)


def _add_websocket_route(self: Any, path: str, resource: Any) -> None:
    """
    Registers a WebSocket resource handler for a given URL path.
    
    Raises:
        ValueError: If a WebSocket route is already registered for the specified path.
    """
    if path in self._websocket_routes:
        msg = f"WebSocket route already registered for path: {path}"
        raise ValueError(msg)

    self._websocket_routes[path] = resource
