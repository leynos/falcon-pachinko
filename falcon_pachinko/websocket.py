"""WebSocket routing utilities and connection management.

This module extends a Falcon application with helpers for registering
``WebSocketResource`` classes under specific route paths and for instantiating
them on demand. It also provides ``WebSocketConnectionManager`` to track active
connections and organize them into rooms. The overall design rationale for this
approach is documented in
``docs/falcon-websocket-extension-design.md``.
"""

from __future__ import annotations

import dataclasses as dc
import typing
from threading import Lock
from types import MethodType

from .resource import WebSocketResource


def _kwargs_factory() -> dict[str, typing.Any]:
    """Return a new kwargs dict with a precise type."""

    return {}


@dc.dataclass(slots=True)
class RouteSpec:
    """Hold configuration for a WebSocket route."""

    resource_cls: type[WebSocketResource]
    args: tuple[typing.Any, ...] = ()
    kwargs: dict[str, typing.Any] = dc.field(default_factory=_kwargs_factory)


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
    routes: dict[str, RouteSpec] = {}
    app._websocket_routes = routes
    app.add_websocket_route = MethodType(_add_websocket_route, app)
    app.create_websocket_resource = MethodType(_create_websocket_resource, app)
    app._websocket_route_lock = Lock()


def _has_whitespace(text: str) -> bool:
    """Return ``True`` if ``text`` contains any whitespace characters."""

    return text != text.strip() or any(ch.isspace() for ch in text)


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

    return False if not path or _has_whitespace(path) else path.startswith("/")


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


def _add_websocket_route(
    self: typing.Any,
    path: str,
    resource_cls: typing.Any,
    *init_args: typing.Any,
    **init_kwargs: typing.Any,
) -> None:
    """
    Registers ``resource_cls`` to handle connections for ``path``.

    Any ``init_args`` or ``init_kwargs`` supplied are stored and applied when
    ``create_websocket_resource`` is called. This allows a single resource class
    to be configured differently across multiple routes.
    """
    _validate_route_path(path)
    _validate_resource_cls(resource_cls)
    with self._websocket_route_lock:
        if path in self._websocket_routes:
            msg = f"WebSocket route already registered for path: {path}"
            raise ValueError(msg)

        self._websocket_routes[path] = RouteSpec(
            resource_cls,
            init_args,
            dict(init_kwargs),
        )


def _create_websocket_resource(self: typing.Any, path: str) -> WebSocketResource:
    """
    Instantiates and returns the WebSocket resource registered for ``path``.

    Initialization parameters provided to :func:`add_websocket_route` are
    forwarded to the resource constructor.

    Args:
        path: The route path for which to create the resource.

    Returns:
        A new instance of the resource associated with ``path``.

    Raises:
        ValueError: If no resource class is registered for ``path``.
    """
    with self._websocket_route_lock:
        routes = self._websocket_routes
        try:
            entry = routes[path]
        except KeyError as exc:
            raise ValueError(
                f"No WebSocket resource registered for path: {path}"
            ) from exc

    return entry.resource_cls(*entry.args, **entry.kwargs)
