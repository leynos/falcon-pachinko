from __future__ import annotations

import typing
from threading import Lock
from types import MethodType


class WebSocketConnectionManager:
    """Track active WebSocket connections."""

    def __init__(self) -> None:
        """Initialise empty connection and room mappings."""
        self.connections: dict[str, typing.Any] = {}
        self.rooms: dict[str, set[str]] = {}


# Public API ---------------------------------------------------------------


def install(app: typing.Any) -> None:
    """Attach WebSocket helpers and routing utilities to ``app``."""
    wanted = (
        "ws_connection_manager",
        "_websocket_routes",
        "add_websocket_route",
        "create_websocket_resource",
    )

    # Idempotent: if all attributes are present, do nothing.
    if all(hasattr(app, name) for name in wanted):
        return

    # If only some attributes are present, raise an error to avoid
    # leaving the app in an inconsistent state.
    if any(hasattr(app, name) for name in wanted):
        raise RuntimeError("Partial WebSocket install detected; aborting.")

    app.ws_connection_manager = WebSocketConnectionManager()
    routes: dict[str, typing.Any] = {}
    app._websocket_routes = routes
    app.add_websocket_route = MethodType(_add_websocket_route, app)
    app.create_websocket_resource = MethodType(_create_websocket_resource, app)


_route_lock = Lock()


def _add_websocket_route(
    self: typing.Any, path: str, resource_cls: type[typing.Any]
) -> None:
    """Register a ``WebSocketResource`` class for the given route."""
    with _route_lock:
        if path in self._websocket_routes:
            msg = f"WebSocket route already registered for path: {path}"
            raise ValueError(msg)

        self._websocket_routes[path] = resource_cls


def _create_websocket_resource(self: typing.Any, path: str) -> typing.Any:
    """Instantiate the resource class registered for ``path``."""
    resource_cls = self._websocket_routes[path]
    return resource_cls()
