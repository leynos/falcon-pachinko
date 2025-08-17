"""WebSocket routing utilities and connection management.

This module extends a Falcon application with helpers for registering
``WebSocketResource`` classes under specific route paths and for instantiating
them on demand. It also provides ``WebSocketConnectionManager`` to track active
connections and organize them into rooms. The overall design rationale for this
approach is documented in :doc:`falcon-websocket-extension-design`.
"""

from __future__ import annotations

import asyncio
import dataclasses as dc
import typing
import warnings
from threading import Lock as ThreadLock
from types import MethodType

from .resource import WebSocketResource

if typing.TYPE_CHECKING:
    from .protocols import WebSocketLike


class PartialWebSocketInstallError(RuntimeError):
    """Raised when WebSocket installation is partially complete.

    This exception is raised when some but not all WebSocket attributes are
    present on the application, indicating an inconsistent installation state.
    """

    def __init__(self) -> None:
        """Initialize the exception."""
        super().__init__("Partial WebSocket install detected; aborting.")


class InvalidWebSocketRoutePathError(ValueError):
    """Raised when an invalid WebSocket route path is provided.

    This exception is raised when attempting to register a WebSocket route
    with a path that doesn't meet the required format (non-empty string
    starting with '/', no whitespace, no leading/trailing whitespace).
    """

    def __init__(self, path: str) -> None:
        """Initialize the exception with the invalid path.

        Parameters
        ----------
        path : str
            The invalid route path that was provided
        """
        super().__init__(f"Invalid WebSocket route path: {path!r}")


class WebSocketResourceNotFoundError(ValueError):
    """Raised when no WebSocket resource is registered for a path.

    This exception is raised when attempting to create a WebSocket resource
    for a path that has not been registered with the application.
    """

    def __init__(self, path: str) -> None:
        """Initialize the exception with the missing path.

        Parameters
        ----------
        path : str
            The path for which no resource is registered
        """
        super().__init__(f"No WebSocket resource registered for path: {path}")


class WebSocketConnectionNotFoundError(KeyError):
    """Raised when referencing an unknown WebSocket connection."""

    def __init__(self, conn_id: str) -> None:
        """Initialize the exception with the missing connection ID."""
        super().__init__(f"Unknown connection ID: {conn_id!r}")


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
    """Track active WebSocket connections and group them into rooms.

    ``connections`` maps each connection ID to a ``falcon.asgi.WebSocket``
    object, allowing other components of the application to send targeted
    messages. ``rooms`` maps room names to sets of connection IDs so that
    multiple clients can be addressed at once. A single connection may join
    any number of rooms.

    The initial implementation is an in-process store, but the class is
    designed to evolve into a pluggable backend so that distributed
    deployments can share state. Implementations MUST guard concurrent
    access. The in-process version is task-safe within a single event loop
    via :class:`asyncio.Lock`; using the same instance across multiple threads
    or event loops is not supported.
    """

    def __init__(self) -> None:
        """Initialize the WebSocketConnectionManager with empty mappings."""
        self.connections: dict[str, WebSocketLike] = {}
        self.rooms: dict[str, set[str]] = {}
        self._lock = asyncio.Lock()

    async def add_connection(self, conn_id: str, ws: WebSocketLike) -> None:
        """Register a new WebSocket connection."""
        async with self._lock:
            self.connections[conn_id] = ws

    async def remove_connection(self, conn_id: str) -> None:
        """Remove a WebSocket connection and purge room memberships."""
        async with self._lock:
            self.connections.pop(conn_id, None)
            empty_rooms: list[str] = []
            for room, members in self.rooms.items():
                members.discard(conn_id)
                if not members:
                    empty_rooms.append(room)
            for room in empty_rooms:
                self.rooms.pop(room, None)

    async def join_room(self, conn_id: str, room: str) -> None:
        """Add a connection to the given room."""
        async with self._lock:
            if conn_id not in self.connections:
                raise WebSocketConnectionNotFoundError(conn_id)
            self.rooms.setdefault(room, set()).add(conn_id)

    async def leave_room(self, conn_id: str, room: str) -> None:
        """Remove a connection from the given room."""
        async with self._lock:
            members = self.rooms.get(room)
            if not members:
                return
            members.discard(conn_id)
            if not members:
                self.rooms.pop(room, None)

    async def send_to_connection(self, conn_id: str, data: object) -> None:
        """Send ``data`` to a specific connection by ID."""
        async with self._lock:
            ws = self.connections.get(conn_id)
        if ws is None:
            raise WebSocketConnectionNotFoundError(conn_id)
        await ws.send_media(data)

    async def broadcast_to_room(
        self,
        room: str,
        data: object,
        *,
        exclude: set[str] | None = None,
    ) -> None:
        """Send ``data`` to every connection in ``room``.

        Parameters
        ----------
        room : str
            Target room name.
        data : object
            Structured data to forward to each connection.
        exclude : set[str] | None, optional
            Connection IDs to skip.
        """
        async with self._lock:
            ids = list(self.rooms.get(room, set()))
            websockets = [
                self.connections[cid]
                for cid in ids
                if (not exclude or cid not in exclude) and cid in self.connections
            ]

        results = await asyncio.gather(
            *(ws.send_media(data) for ws in websockets),
            return_exceptions=True,
        )
        for exc in results:
            if isinstance(exc, Exception):
                raise exc


# Public API ---------------------------------------------------------------


def install(app: typing.Any) -> None:  # noqa: ANN401
    """Attach WebSocket connection management and routing utilities to the app.

    Initializes and binds WebSocket-related attributes and methods to the given app,
    enabling WebSocket route registration and resource instantiation. This function
    is idempotent and will raise a RuntimeError if a partial installation is detected.

    Parameters
    ----------
    app : typing.Any
        The application object to install WebSocket support on
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
        raise PartialWebSocketInstallError

    app.ws_connection_manager = WebSocketConnectionManager()
    routes: dict[str, RouteSpec] = {}
    app._websocket_routes = routes
    app.add_websocket_route = MethodType(_add_websocket_route, app)
    app.create_websocket_resource = MethodType(_create_websocket_resource, app)
    app._websocket_route_lock = ThreadLock()


def _has_whitespace(text: str) -> bool:
    """Return ``True`` if ``text`` contains any whitespace characters.

    Parameters
    ----------
    text : str
        The text to check for whitespace

    Returns
    -------
    bool
        True if text contains any whitespace characters, False otherwise
    """
    return text != text.strip() or any(ch.isspace() for ch in text)


def _is_valid_route_path(path: object) -> bool:
    """Check if the given path is a valid WebSocket route path.

    A valid route path is a non-empty string that starts with '/', contains no
    leading or trailing whitespace, and has no internal whitespace characters.

    Parameters
    ----------
    path : object
        The value to check

    Returns
    -------
    bool
        True if the path is a valid WebSocket route path, False otherwise
    """
    if not isinstance(path, str):
        return False

    return False if not path or _has_whitespace(path) else path.startswith("/")


def _validate_route_path(path: object) -> None:
    """Validate that the given path is suitable for use as a WebSocket route.

    Parameters
    ----------
    path : object
        The path to validate

    Raises
    ------
    ValueError
        If the path is not a non-empty string starting with '/', contains
        whitespace, or has leading/trailing whitespace
    """
    if not _is_valid_route_path(path):
        raise InvalidWebSocketRoutePathError(str(path))


def _validate_resource_cls(resource_cls: object) -> None:
    """Validate that the provided class is a subclass of WebSocketResource.

    Parameters
    ----------
    resource_cls : object
        The class to validate

    Raises
    ------
    TypeError
        If resource_cls is not a subclass of WebSocketResource
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
    self: typing.Any,  # noqa: ANN401
    path: str,
    resource_cls: object,
    *init_args: object,
    **init_kwargs: object,
) -> None:
    """Register ``resource_cls`` to handle connections for ``path``.

    .. deprecated:: 0.1
       Use :class:`falcon_pachinko.router.WebSocketRouter` instead.

    Any ``init_args`` or ``init_kwargs`` supplied are stored and applied when
    ``create_websocket_resource`` is called. This allows a single resource class
    to be configured differently across multiple routes.

    Parameters
    ----------
    self : typing.Any
        The application instance
    path : str
        The WebSocket route path
    resource_cls : object
        The WebSocketResource subclass to register
    *init_args : object
        Positional arguments for resource initialization
    **init_kwargs : object
        Keyword arguments for resource initialization
    """
    warnings.warn(
        "_add_websocket_route is deprecated; use WebSocketRouter.add_route instead",
        DeprecationWarning,
        stacklevel=2,
    )
    _validate_route_path(path)
    _validate_resource_cls(resource_cls)
    with self._websocket_route_lock:
        if path in self._websocket_routes:
            msg = f"WebSocket route already registered for path: {path}"
            raise ValueError(msg)

        self._websocket_routes[path] = RouteSpec(
            typing.cast("type[WebSocketResource]", resource_cls),
            init_args,
            dict(init_kwargs),
        )


def _create_websocket_resource(self: typing.Any, path: str) -> WebSocketResource:  # noqa: ANN401
    """Instantiate and return the WebSocket resource registered for ``path``.

    Initialization parameters provided to :func:`add_websocket_route` are
    forwarded to the resource constructor.

    .. deprecated:: 0.1
       Use :class:`falcon_pachinko.router.WebSocketRouter` instead.

    Parameters
    ----------
    self : typing.Any
        The application instance
    path : str
        The route path for which to create the resource

    Returns
    -------
    WebSocketResource
        A new instance of the resource associated with ``path``

    Raises
    ------
    ValueError
        If no resource class is registered for ``path``
    """
    warnings.warn(
        "_create_websocket_resource is deprecated; use WebSocketRouter instead",
        DeprecationWarning,
        stacklevel=2,
    )
    with self._websocket_route_lock:
        routes = self._websocket_routes
        try:
            entry = routes[path]
        except KeyError as exc:
            raise WebSocketResourceNotFoundError(path) from exc

    return entry.resource_cls(*entry.args, **entry.kwargs)
