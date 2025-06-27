"""Tests for the WebSocket application installation functionality.

This module contains comprehensive tests for the install() function and related
WebSocket integration features, including route registration, resource creation,
and application state management.
"""
from __future__ import annotations

import typing
from threading import Lock

import pytest

from falcon_pachinko import install
from falcon_pachinko.resource import WebSocketResource
from falcon_pachinko.websocket import RouteSpec, WebSocketConnectionManager


class DummyApp:
    """A minimal dummy application class for testing WebSocket installation."""

    pass


class SupportsWebSocket(typing.Protocol):
    """Protocol defining the interface for applications with WebSocket support."""

    ws_connection_manager: WebSocketConnectionManager
    _websocket_routes: dict[str, RouteSpec]
    _websocket_route_lock: Lock

    def create_websocket_resource(self, path: str) -> object:
        """Create and return a new instance of the WebSocket resource class.

        Creates a new instance of the WebSocket resource class registered for the
        specified path.

        Parameters
        ----------
        path : str
            The WebSocket route path for which to create a resource instance

        Returns
        -------
        object
            A new instance of the resource class associated with the given path

        Raises
        ------
        ValueError
            If no resource class is registered for the specified path
        """
        ...

    def add_websocket_route(
        self, path: str, resource: type[object], *args: typing.Any, **kwargs: typing.Any
    ) -> None:
        """Register a WebSocketResource subclass to handle connections.

        Registers a WebSocketResource subclass to handle connections at the
        specified path.

        Parameters
        ----------
        path : str
            The WebSocket route path to register (must be a non-empty string
            starting with '/')
        resource : type[object]
            The class of the WebSocketResource to associate with the path
        *args : typing.Any
            Positional arguments used when instantiating the resource
        **kwargs : typing.Any
            Keyword arguments used when instantiating the resource

        Raises
        ------
        ValueError
            If the path is invalid or already registered
        TypeError
            If resource is not a subclass of WebSocketResource
        """
        ...


@pytest.fixture
def dummy_app() -> SupportsWebSocket:
    """Create a dummy application instance with WebSocket support installed.

    Returns
    -------
        The dummy app instance cast to the SupportsWebSocket protocol, with
        WebSocket integration methods and attributes added
    """
    app = DummyApp()
    install(app)  # type: ignore[arg-type]
    return typing.cast("SupportsWebSocket", app)


@pytest.fixture
def dummy_resource_cls(dummy_app: SupportsWebSocket) -> type[WebSocketResource]:
    """Create and return a dummy WebSocketResource subclass for testing purposes.

    Parameters
    ----------
    dummy_app : SupportsWebSocket
        The application instance supporting WebSocket features

    Returns
    -------
    type[WebSocketResource]
        A subclass of WebSocketResource named DummyResource
    """

    class DummyResource(WebSocketResource):
        pass

    return DummyResource


def test_install_adds_methods_and_manager(dummy_app: SupportsWebSocket) -> None:
    """Verify that the install() function adds WebSocket-related attributes and methods.

    Verifies that the install() function adds WebSocket-related attributes and
    methods to the app, including the connection manager, route registration,
    resource creation, and locking mechanism.
    """
    app_any = dummy_app

    assert hasattr(app_any, "ws_connection_manager")
    assert isinstance(app_any.ws_connection_manager, WebSocketConnectionManager)
    assert callable(app_any.add_websocket_route)
    assert callable(app_any.create_websocket_resource)
    assert hasattr(app_any, "_websocket_route_lock")
    assert isinstance(app_any._websocket_route_lock, Lock)  # pyright: ignore[reportPrivateUsage]


def test_add_websocket_route_registers_resource(
    dummy_app: SupportsWebSocket, dummy_resource_cls: type[WebSocketResource]
) -> None:
    """Test that `add_websocket_route` registers the given resource class.

    Tests that `add_websocket_route` registers the given resource class under the
    specified path in the app's internal WebSocket routes mapping.
    """
    dummy_app.add_websocket_route("/ws", dummy_resource_cls, 1, flag=True)

    stored = dummy_app._websocket_routes["/ws"]  # pyright: ignore[reportPrivateUsage]
    assert stored.resource_cls is dummy_resource_cls
    assert stored.args == (1,)
    assert stored.kwargs == {"flag": True}


def test_install_is_idempotent(dummy_app: SupportsWebSocket) -> None:
    """Verify that calling install() multiple times does not alter existing attributes.

    Verifies that calling install() multiple times does not alter or replace
    existing WebSocket-related attributes and methods on the app.
    """
    first_manager = dummy_app.ws_connection_manager
    first_route_fn = dummy_app.add_websocket_route
    first_create_fn = dummy_app.create_websocket_resource
    first_lock = dummy_app._websocket_route_lock  # pyright: ignore[reportPrivateUsage]

    install(dummy_app)  # type: ignore[arg-type]
    assert dummy_app.ws_connection_manager is first_manager
    assert dummy_app.add_websocket_route is first_route_fn
    assert dummy_app.create_websocket_resource is first_create_fn
    assert dummy_app._websocket_route_lock is first_lock  # pyright: ignore[reportPrivateUsage]


def test_install_detects_partial_state(dummy_app: SupportsWebSocket) -> None:
    """Test that `install()` raises a RuntimeError if required attributes are missing.

    Tests that `install()` raises a RuntimeError if required WebSocket-related
    attributes are missing from the app, indicating a corrupted or incomplete
    installation state.
    """
    # Simulate tampering with one of the install attributes
    delattr(dummy_app, "_websocket_routes")
    delattr(dummy_app, "create_websocket_resource")
    delattr(dummy_app, "_websocket_route_lock")

    with pytest.raises(RuntimeError):
        install(dummy_app)  # type: ignore[arg-type]


def test_add_websocket_route_duplicate_raises(
    dummy_app: SupportsWebSocket, dummy_resource_cls: type[WebSocketResource]
) -> None:
    """Test that registering a WebSocket route for an existing path raises error."""
    dummy_app.add_websocket_route("/ws", dummy_resource_cls)

    with pytest.raises(ValueError, match="already registered"):
        dummy_app.add_websocket_route("/ws", dummy_resource_cls)


@pytest.mark.parametrize(
    "path",
    ["ws", "", " /ws", "/ws ", "/ws\n", 123],
)
def test_add_websocket_route_invalid_path(
    dummy_app: SupportsWebSocket,
    dummy_resource_cls: type[WebSocketResource],
    path: object,
) -> None:
    """
    Tests that add_websocket_route raises ValueError when given an invalid path.

    Verifies that attempting to register a WebSocket route with an invalid path value
    (such as missing a leading slash, being empty, containing only whitespace, or
    being a non-string)
    results in a ValueError.
    """
    with pytest.raises(ValueError, match="Invalid WebSocket route path"):
        dummy_app.add_websocket_route(path, dummy_resource_cls)  # type: ignore[arg-type]


def test_create_websocket_resource_returns_new_instances(
    dummy_app: SupportsWebSocket, dummy_resource_cls: type[WebSocketResource]
) -> None:
    """Test that create_websocket_resource returns new, distinct instances.

    Tests that create_websocket_resource returns new, distinct instances of the
    registered resource class for a given WebSocket path.
    """
    dummy_app.add_websocket_route("/ws", dummy_resource_cls)

    first = dummy_app.create_websocket_resource("/ws")
    second = dummy_app.create_websocket_resource("/ws")

    assert isinstance(first, dummy_resource_cls)
    assert isinstance(second, dummy_resource_cls)
    assert type(first) is dummy_resource_cls
    assert first is not second


def test_route_specific_init_args(dummy_app: SupportsWebSocket) -> None:
    """Test that WebSocket resources are initialized with route-specific arguments."""
    class ConfigResource(WebSocketResource):
        def __init__(self, value: int) -> None:
            self.value = value

    dummy_app.add_websocket_route("/one", ConfigResource, 1)
    dummy_app.add_websocket_route("/two", ConfigResource, 2)

    r1 = typing.cast("ConfigResource", dummy_app.create_websocket_resource("/one"))
    r2 = typing.cast("ConfigResource", dummy_app.create_websocket_resource("/two"))

    assert r1.value == 1
    assert r2.value == 2


def test_create_websocket_resource_unregistered_path(
    dummy_app: SupportsWebSocket,
) -> None:
    """Test that creating a WebSocket resource for an unregistered path raises error."""
    with pytest.raises(ValueError, match="No WebSocket resource registered"):
        dummy_app.create_websocket_resource("/missing")


def test_add_websocket_route_type_check(dummy_app: SupportsWebSocket) -> None:
    """Test that add_websocket_route raises TypeError when given
    non-WebSocketResource.
    """
    with pytest.raises(TypeError):
        dummy_app.add_websocket_route("/ws", object)  # type: ignore[arg-type]
