from __future__ import annotations

import typing

import pytest

from falcon_pachinko import install
from falcon_pachinko.resource import WebSocketResource
from falcon_pachinko.websocket import WebSocketConnectionManager


class DummyApp:
    pass


class SupportsWebSocket(typing.Protocol):
    ws_connection_manager: WebSocketConnectionManager
    _websocket_routes: dict[str, object]

    def create_websocket_resource(self, path: str) -> object:
        """
        Creates and returns a new instance of the WebSocket resource class registered for the specified path.
        
        Args:
            path: The WebSocket route path for which to create a resource instance.
        
        Returns:
            A new instance of the resource class associated with the given path.
        """
        ...

    def add_websocket_route(self, path: str, resource: type[object]) -> None:
        """
        Registers a WebSocketResource subclass to handle connections at the specified path.
        
        Args:
            path: The URL path for the WebSocket endpoint.
            resource: The class of the WebSocketResource to associate with the path.
        
        Raises:
            ValueError: If a resource is already registered for the given path.
        """
        ...


def test_install_adds_methods_and_manager() -> None:
    """
    Tests that the install() function adds WebSocket management attributes and methods to the app.
    
    Verifies that the app gains a ws_connection_manager of the correct type, and that add_websocket_route and create_websocket_resource methods are attached and callable.
    """
    app = DummyApp()
    install(app)  # type: ignore[arg-type]
    app_any = typing.cast("SupportsWebSocket", app)

    assert hasattr(app_any, "ws_connection_manager")
    assert isinstance(app_any.ws_connection_manager, WebSocketConnectionManager)
    assert callable(app_any.add_websocket_route)
    assert callable(app_any.create_websocket_resource)


def test_add_websocket_route_registers_resource() -> None:
    """``add_websocket_route`` stores the resource class for the path."""
    app = DummyApp()
    install(app)  # type: ignore[arg-type]
    app_any = typing.cast("SupportsWebSocket", app)

    class R(WebSocketResource):
        pass

    app_any.add_websocket_route("/ws", R)

    assert app_any._websocket_routes["/ws"] is R  # pyright: ignore[reportPrivateUsage]


def test_install_is_idempotent() -> None:
    """
    Verifies that calling install() multiple times on the same app instance does not alter or replace existing WebSocket-related attributes or methods.
    """
    app = DummyApp()
    install(app)  # type: ignore[arg-type]
    app_any = typing.cast("SupportsWebSocket", app)
    first_manager = app_any.ws_connection_manager
    first_route_fn = app_any.add_websocket_route
    first_create_fn = app_any.create_websocket_resource

    install(app)  # type: ignore[arg-type]
    assert app_any.ws_connection_manager is first_manager
    assert app_any.add_websocket_route is first_route_fn
    assert app_any.create_websocket_resource is first_create_fn


def test_install_detects_partial_state() -> None:
    """
    Tests that `install()` raises a RuntimeError if required WebSocket attributes are missing from the app, indicating a corrupted installation state.
    """
    app = DummyApp()
    install(app)  # type: ignore[arg-type]
    app_any = typing.cast("SupportsWebSocket", app)

    # Simulate tampering with one of the install attributes
    delattr(app_any, "_websocket_routes")
    delattr(app_any, "create_websocket_resource")

    with pytest.raises(RuntimeError):
        install(app)  # type: ignore[arg-type]


def test_add_websocket_route_duplicate_raises() -> None:
    """
    Tests that adding a duplicate WebSocket route raises a ValueError.
    
    Verifies that attempting to register the same route path with the same resource class more than once results in an error.
    """
    app = DummyApp()
    install(app)  # type: ignore[arg-type]
    app_any = typing.cast("SupportsWebSocket", app)

    class R(WebSocketResource):
        pass

    app_any.add_websocket_route("/ws", R)

    with pytest.raises(ValueError):
        app_any.add_websocket_route("/ws", R)


def test_create_websocket_resource_returns_new_instances() -> None:
    """
    Tests that create_websocket_resource returns distinct new instances of the registered resource class for a given WebSocket route.
    """
    app = DummyApp()
    install(app)  # type: ignore[arg-type]
    app_any = typing.cast("SupportsWebSocket", app)

    class R(WebSocketResource):
        pass

    app_any.add_websocket_route("/ws", R)

    first = app_any.create_websocket_resource("/ws")
    second = app_any.create_websocket_resource("/ws")

    assert isinstance(first, R)
    assert isinstance(second, R)
    assert first is not second
