from __future__ import annotations

from threading import Lock
from types import MethodType
from typing import Any


class WebSocketConnectionManager:
    """Track active WebSocket connections."""

    def __init__(self) -> None:
        """
        Initialises the WebSocketConnectionManager with empty connection and room mappings.
        
        Creates dictionaries to track active WebSocket connections and group them into rooms.
        """
        self.connections: dict[str, Any] = {}
        self.rooms: dict[str, set[str]] = {}


# Public API ---------------------------------------------------------------


def install(app: Any) -> None:
    """
    Attaches WebSocket management utilities to a Falcon app instance.
    
    Initialises and binds a WebSocket connection manager, a route mapping dictionary, and a method for registering WebSocket routes to the app. If the app already has all required WebSocket attributes, the function does nothing. If only some attributes are present, raises a RuntimeError to prevent inconsistent state.
    """
    wanted = (
        "ws_connection_manager",
        "_websocket_routes",
        "add_websocket_route",
    )

    # Idempotent: if all attributes are present, do nothing.
    if all(hasattr(app, name) for name in wanted):
        return

    # If only some attributes are present, raise an error to avoid
    # leaving the app in an inconsistent state.
    if any(hasattr(app, name) for name in wanted):
        raise RuntimeError("Partial WebSocket install detected; aborting.")

    app.ws_connection_manager = WebSocketConnectionManager()
    routes: dict[str, Any] = {}
    app._websocket_routes = routes
    app.add_websocket_route = MethodType(_add_websocket_route, app)


_route_lock = Lock()


def _add_websocket_route(self: Any, path: str, resource: Any) -> None:
    """
    Registers a WebSocket resource handler for a specified path on the application.
    
    Ensures thread-safe registration and raises a ValueError if the path is already registered.
    """
    with _route_lock:
        if path in self._websocket_routes:
            msg = f"WebSocket route already registered for path: {path}"
            raise ValueError(msg)

        self._websocket_routes[path] = resource
