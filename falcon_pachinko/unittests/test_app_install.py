from __future__ import annotations

import typing

import pytest

from falcon_pachinko import install
from falcon_pachinko.websocket import WebSocketConnectionManager


class DummyApp:
    pass


class SupportsWebSocket(typing.Protocol):
    ws_connection_manager: WebSocketConnectionManager
    _websocket_routes: dict[str, object]

    def add_websocket_route(self, path: str, resource: object) -> None:
        """
        Registers a resource to handle WebSocket connections at the specified path.
        
        Raises:
            ValueError: If a resource is already registered for the given path.
        """
        ...


def test_install_adds_methods_and_manager() -> None:
    """Tests that installing WebSocket support adds the connection manager and route registration method to the app."""
    app = DummyApp()
    install(app)  # type: ignore[arg-type]
    app_any = typing.cast("SupportsWebSocket", app)

    assert hasattr(app_any, "ws_connection_manager")
    assert isinstance(app_any.ws_connection_manager, WebSocketConnectionManager)
    assert callable(app_any.add_websocket_route)


def test_add_websocket_route_registers_resource() -> None:
    """
    Tests that add_websocket_route registers a resource under the specified WebSocket path.
    """
    app = DummyApp()
    install(app)  # type: ignore[arg-type]
    app_any = typing.cast("SupportsWebSocket", app)

    resource = object()
    app_any.add_websocket_route("/ws", resource)

    assert app_any._websocket_routes["/ws"] is resource  # pyright: ignore[reportPrivateUsage]


def test_install_is_idempotent() -> None:
    """
    Tests that installing WebSocket support multiple times does not alter the app's WebSocket state.
    """
    app = DummyApp()
    install(app)  # type: ignore[arg-type]
    app_any = typing.cast("SupportsWebSocket", app)
    first_manager = app_any.ws_connection_manager
    first_route_fn = app_any.add_websocket_route

    install(app)  # type: ignore[arg-type]
    assert app_any.ws_connection_manager is first_manager
    assert app_any.add_websocket_route is first_route_fn


def test_install_detects_partial_state() -> None:
    """
    Tests that install raises RuntimeError if required internal state is missing from the app.
    """
    app = DummyApp()
    install(app)  # type: ignore[arg-type]
    app_any = typing.cast("SupportsWebSocket", app)

    # Simulate tampering with one of the install attributes
    delattr(app_any, "_websocket_routes")

    with pytest.raises(RuntimeError):
        install(app)  # type: ignore[arg-type]


def test_add_websocket_route_duplicate_raises() -> None:
    app = DummyApp()
    install(app)  # type: ignore[arg-type]
    app_any = typing.cast("SupportsWebSocket", app)

    resource = object()
    app_any.add_websocket_route("/ws", resource)

    with pytest.raises(ValueError):
        app_any.add_websocket_route("/ws", resource)
