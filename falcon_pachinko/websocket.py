from __future__ import annotations

import typing
from threading import Lock
from types import MethodType

from .resource import WebSocketResource


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
        "_websocket_route_lock",
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
    app._websocket_route_lock = Lock()


def _is_valid_route_path(path: typing.Any) -> bool:
    """Return ``True`` if ``path`` looks like a WebSocket route."""

    if not isinstance(path, str):
        return False

    if not path or path != path.strip() or any(ch.isspace() for ch in path):
        return False

    return path.startswith("/")


def _validate_route_path(path: typing.Any) -> None:
    """Validate ``path`` for use as a WebSocket route.

    Rules enforced:
    - ``path`` must be a string.
    - it may not be empty or contain any whitespace characters.
    - leading and trailing whitespace are not allowed.
    - it must start with ``/``.
    """

    if not _is_valid_route_path(path):
        raise ValueError(f"Invalid WebSocket route path: {path!r}")


def _validate_resource_cls(resource_cls: typing.Any) -> None:
    """Ensure ``resource_cls`` is a ``WebSocketResource`` subclass."""
    if not isinstance(resource_cls, type) or not issubclass(
        resource_cls,
        WebSocketResource,
    ):
        msg = (
            "resource_cls must be a subclass of WebSocketResource, got "
            f"{resource_cls!r}"
        )
        raise TypeError(msg)


def _add_websocket_route(self: typing.Any, path: str, resource_cls: typing.Any) -> None:
    """Register a ``WebSocketResource`` subclass for ``path``."""
    _validate_route_path(path)
    _validate_resource_cls(resource_cls)
    with self._websocket_route_lock:
        if path in self._websocket_routes:
            msg = f"WebSocket route already registered for path: {path}"
            raise ValueError(msg)

        self._websocket_routes[path] = resource_cls


def _create_websocket_resource(self: typing.Any, path: str) -> WebSocketResource:
    """Instantiate the resource class registered for ``path``."""
    with self._websocket_route_lock:
        try:
            resource_cls = self._websocket_routes[path]
        except KeyError as exc:
            raise ValueError(
                f"No WebSocket resource registered for path: {path}"
            ) from exc

    return resource_cls()
