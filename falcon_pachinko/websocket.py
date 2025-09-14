"""WebSocket routing utilities and connection management.

This module extends a Falcon application with helpers for registering
``WebSocketResource`` classes under specific route paths and for instantiating
them on demand. It also provides ``WebSocketConnectionManager`` to track active
connections and organize them into rooms. The overall design rationale for this
approach is documented in :doc:`falcon-websocket-extension-design`.
"""

from __future__ import annotations

import abc
import asyncio
import dataclasses as dc
import types
import typing as typ
import warnings
from threading import Lock as ThreadLock
from types import MethodType

from .resource import WebSocketResource

if typ.TYPE_CHECKING:
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


def _kwargs_factory() -> dict[str, typ.Any]:
    """Return a new kwargs dict with a precise type."""
    return {}


@dc.dataclass(slots=True)
class RouteSpec:
    """Hold configuration for a WebSocket route."""

    resource_cls: type[WebSocketResource]
    args: tuple[typ.Any, ...] = ()
    kwargs: dict[str, typ.Any] = dc.field(default_factory=_kwargs_factory)


class ConnectionBackend(abc.ABC):
    """
    Abstract interface for connection manager backends.

    Thread-safety expectations:
        Implementations of this interface are not required to be thread-safe
        by default. Backends intended for multi-threaded or distributed use
        must provide their own synchronization. Users should consult backend
        documentation before relying on concurrent scenarios.
    """

    @property
    @abc.abstractmethod
    def websockets(self) -> typ.Mapping[str, WebSocketLike]:
        """Mapping of connection IDs to local WebSocket objects."""

    @property
    @abc.abstractmethod
    def rooms(self) -> typ.Mapping[str, typ.Collection[str]]:
        """Read-only mapping of room names to connection ID collections."""

    @abc.abstractmethod
    async def add_connection(self, conn_id: str, ws: WebSocketLike) -> None:
        """Register a new connection."""

    @abc.abstractmethod
    async def remove_connection(self, conn_id: str) -> None:
        """Remove a connection and purge memberships."""

    @abc.abstractmethod
    async def join_room(self, conn_id: str, room: str) -> None:
        """Add ``conn_id`` to ``room``."""

    @abc.abstractmethod
    async def leave_room(self, conn_id: str, room: str) -> None:
        """Remove ``conn_id`` from ``room``."""

    @abc.abstractmethod
    async def get_websocket(self, conn_id: str) -> WebSocketLike | None:
        """Return websocket for ``conn_id`` if present."""

    @abc.abstractmethod
    async def snapshot(
        self, room: str | None = None
    ) -> list[tuple[str, WebSocketLike]]:
        """Return snapshot of (conn_id, websocket) pairs."""


class InProcessBackend(ConnectionBackend):
    """Task-safe in-memory backend."""

    def __init__(self) -> None:
        self._websockets: dict[str, WebSocketLike] = {}
        self._rooms: dict[str, set[str]] = {}
        self._lock = asyncio.Lock()

    @property
    def websockets(self) -> typ.Mapping[str, WebSocketLike]:
        """Read-only mapping of connection IDs to WebSocket objects."""
        return types.MappingProxyType(self._websockets)

    @property
    def rooms(self) -> typ.Mapping[str, frozenset[str]]:
        """Read-only mapping of room names to member IDs."""
        snapshot = {room: frozenset(ids) for room, ids in self._rooms.items()}
        return types.MappingProxyType(snapshot)

    async def add_connection(self, conn_id: str, ws: WebSocketLike) -> None:
        """Register a new connection."""
        async with self._lock:
            if conn_id in self._websockets:
                msg = f"Duplicate connection ID: {conn_id!r}"
                raise ValueError(msg)
            self._websockets[conn_id] = ws

    async def remove_connection(self, conn_id: str) -> None:
        """Remove a connection and clean up room memberships."""
        async with self._lock:
            self._websockets.pop(conn_id, None)
            empty: list[str] = []
            for room, members in self._rooms.items():
                members.discard(conn_id)
                if not members:
                    empty.append(room)
            for room in empty:
                self._rooms.pop(room, None)

    async def join_room(self, conn_id: str, room: str) -> None:
        """Add ``conn_id`` to ``room``."""
        async with self._lock:
            if conn_id not in self._websockets:
                raise WebSocketConnectionNotFoundError(conn_id)
            self._rooms.setdefault(room, set()).add(conn_id)

    async def leave_room(self, conn_id: str, room: str) -> None:
        """Remove ``conn_id`` from ``room`` if present."""
        async with self._lock:
            members = self._rooms.get(room)
            if not members:
                return
            members.discard(conn_id)
            if not members:
                self._rooms.pop(room, None)

    async def get_websocket(self, conn_id: str) -> WebSocketLike | None:
        """Return websocket for ``conn_id`` if known."""
        async with self._lock:
            return self._websockets.get(conn_id)

    async def snapshot(
        self, room: str | None = None
    ) -> list[tuple[str, WebSocketLike]]:
        """Return snapshot of websockets for ``room`` or all."""
        async with self._lock:
            if room is None:
                items = list(self._websockets.items())
            else:
                ids = list(self._rooms.get(room, set()))
                items = []
                for cid in ids:
                    ws = self._websockets.get(cid)
                    if ws is None:
                        raise WebSocketConnectionNotFoundError(cid)
                    items.append((cid, ws))
        return items


class WebSocketConnectionManager:
    """Track active WebSocket connections and group them into rooms.

    WebSocketConnectionManager now delegates storage to a pluggable backend
    so that deployments may swap in distributed implementations without
    changing application code.
    """

    def __init__(self, backend: ConnectionBackend | None = None) -> None:
        self._backend = backend or InProcessBackend()

    @property
    def websockets(self) -> typ.Mapping[str, WebSocketLike]:
        """Expose backend websocket mapping."""
        return self._backend.websockets

    @property
    def rooms(self) -> typ.Mapping[str, typ.Collection[str]]:
        """Expose backend room membership mapping."""
        return self._backend.rooms

    async def add_connection(self, conn_id: str, ws: WebSocketLike) -> None:
        """Register a connection with the backend."""
        await self._backend.add_connection(conn_id, ws)

    async def remove_connection(self, conn_id: str) -> None:
        """Forget a connection and its room memberships."""
        await self._backend.remove_connection(conn_id)

    async def join_room(self, conn_id: str, room: str) -> None:
        """Add an existing connection to ``room``."""
        await self._backend.join_room(conn_id, room)

    async def leave_room(self, conn_id: str, room: str) -> None:
        """Remove ``conn_id`` from ``room`` if present."""
        await self._backend.leave_room(conn_id, room)

    async def send_to_connection(self, conn_id: str, data: object) -> None:
        """Send ``data`` to a single connection."""
        ws = await self._backend.get_websocket(conn_id)
        if ws is None:
            raise WebSocketConnectionNotFoundError(conn_id)
        await ws.send_media(data)

    async def broadcast_to_room(
        self,
        room: str,
        data: object,
        *,
        exclude: typ.Collection[str] | None = None,
    ) -> None:
        """Broadcast ``data`` to members of ``room``."""
        snapshot = await self._backend.snapshot(room)
        excluded = set(exclude) if exclude else set()
        websockets = [ws for cid, ws in snapshot if cid not in excluded]

        results = await asyncio.gather(
            *(ws.send_media(data) for ws in websockets),
            return_exceptions=True,
        )
        errors = [exc for exc in results if isinstance(exc, Exception)]
        self._handle_broadcast_errors(errors)

    def _handle_broadcast_errors(self, errors: list[Exception]) -> None:
        if not errors:
            return
        if len(errors) == 1:
            raise errors[0]
        self._raise_exception_group(errors)

    def _raise_exception_group(self, errors: list[Exception]) -> None:
        try:
            msg = "broadcast_to_room errors"
            raise ExceptionGroup(msg, errors)  # type: ignore[name-defined]
        except NameError:  # pragma: no cover - fallback for older Pythons
            raise errors[0] from None

    async def connections(
        self,
        *,
        room: str | None = None,
        exclude: typ.Collection[str] | None = None,
    ) -> typ.AsyncIterator[WebSocketLike]:
        """Iterate over active connections matching ``room`` and ``exclude``."""
        snapshot = await self._backend.snapshot(room)
        excluded = set(exclude) if exclude else set()
        for cid, ws in snapshot:
            if cid not in excluded:
                yield ws


def install(app: typ.Any) -> None:  # noqa: ANN401
    """Attach WebSocket connection management and routing utilities to the app.

    Initializes and binds WebSocket-related attributes and methods to the given app,
    enabling WebSocket route registration and resource instantiation. This function
    is idempotent and will raise a RuntimeError if a partial installation is detected.

    Parameters
    ----------
    app : typ.Any
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
    self: typ.Any,  # noqa: ANN401
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
    self : typ.Any
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
            typ.cast("type[WebSocketResource]", resource_cls),
            init_args,
            dict(init_kwargs),
        )


def _create_websocket_resource(self: typ.Any, path: str) -> WebSocketResource:  # noqa: ANN401
    """Instantiate and return the WebSocket resource registered for ``path``.

    Initialization parameters provided to :func:`add_websocket_route` are
    forwarded to the resource constructor.

    .. deprecated:: 0.1
       Use :class:`falcon_pachinko.router.WebSocketRouter` instead.

    Parameters
    ----------
    self : typ.Any
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
