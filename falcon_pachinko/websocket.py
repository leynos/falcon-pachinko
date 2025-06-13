from __future__ import annotations

import typing
from threading import Lock
from types import MethodType

from .resource import WebSocketResource


class WebSocketConnectionManager:
    """Track active WebSocket connections."""

    def __init__(self) -> None:
        """
        Initializes the WebSocketConnectionManager with empty connection and room mappings.
        """
        self.connections: dict[str, typing.Any] = {}
        self.rooms: dict[str, set[str]] = {}


# Public API ---------------------------------------------------------------


def install(app: typing.Any) -> None:
    """
    Attaches WebSocket connection management and routing utilities to the application object.
    
    Initializes and binds WebSocket-related attributes and methods to the given app, enabling WebSocket route registration and resource instantiation. This function is idempotent and will raise a RuntimeError if a partial installation is detected.
    """
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
    """
    Checks if the given path is a valid WebSocket route path.
    
    A valid route path is a non-empty string that starts with '/', contains no leading or trailing whitespace, and has no internal whitespace characters.
    
    Args:
        path: The value to check.
    
    Returns:
        True if the path is a valid WebSocket route path, False otherwise.
    """

    if not isinstance(path, str):
        return False

    if not path or path != path.strip() or any(ch.isspace() for ch in path):
        return False

    return path.startswith("/")


def _validate_route_path(path: typing.Any) -> None:
    """
    Validates that the given path is suitable for use as a WebSocket route.
    
    Raises:
        ValueError: If the path is not a non-empty string starting with '/', contains whitespace, or has leading/trailing whitespace.
    """

    if not _is_valid_route_path(path):
        raise ValueError(f"Invalid WebSocket route path: {path!r}")


def _validate_resource_cls(resource_cls: typing.Any) -> None:
    """
    Validates that the provided class is a subclass of WebSocketResource.
    
    Raises:
        TypeError: If resource_cls is not a subclass of WebSocketResource.
    """
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
    """
    Registers a WebSocketResource subclass for the specified route path.
    
    Associates the given resource class with the provided path, ensuring the path is valid and not already registered. Raises a ValueError if the path is already in use.
    """
    _validate_route_path(path)
    _validate_resource_cls(resource_cls)
    with self._websocket_route_lock:
        if path in self._websocket_routes:
            msg = f"WebSocket route already registered for path: {path}"
            raise ValueError(msg)

        self._websocket_routes[path] = resource_cls


def _create_websocket_resource(self: typing.Any, path: str) -> WebSocketResource:
    """
    Instantiates and returns the WebSocket resource class registered for the given path.
    
    Args:
        path: The route path for which to create the WebSocket resource.
    
    Returns:
        An instance of the resource class associated with the specified path.
    
    Raises:
        ValueError: If no resource class is registered for the given path.
    """
    with self._websocket_route_lock:
        try:
            resource_cls = self._websocket_routes[path]
        except KeyError as exc:
            raise ValueError(
                f"No WebSocket resource registered for path: {path}"
            ) from exc

    return resource_cls()
