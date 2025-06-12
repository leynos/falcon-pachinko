from __future__ import annotations

from typing import Protocol, cast

import pytest

from falcon_pachinko import install
from falcon_pachinko.websocket import WebSocketConnectionManager


class DummyApp:
    pass


class SupportsWebSocket(Protocol):
    ws_connection_manager: WebSocketConnectionManager
    _websocket_routes: dict[str, object]

    def add_websocket_route(self, path: str, resource: object) -> None:
        """
        Registers a WebSocket resource to handle connections at the specified path.
        
        Args:
            path: The URL path for the WebSocket route.
            resource: The resource object that will handle WebSocket connections for the given path.
        
        Raises:
            ValueError: If a resource is already registered for the specified path.
        """
        ...


def test_install_adds_methods_and_manager() -> None:
    app = DummyApp()
    install(app)  # type: ignore[arg-type]
    app_any = cast(SupportsWebSocket, app)

    assert hasattr(app_any, "ws_connection_manager")
    assert isinstance(app_any.ws_connection_manager, WebSocketConnectionManager)
    assert callable(app_any.add_websocket_route)


def test_add_websocket_route_registers_resource() -> None:
    app = DummyApp()
    install(app)  # type: ignore[arg-type]
    app_any = cast(SupportsWebSocket, app)

    resource = object()
    app_any.add_websocket_route("/ws", resource)

    assert app_any._websocket_routes["/ws"] is resource  # pyright: ignore[reportPrivateUsage]


def test_install_is_idempotent() -> None:
    """
    Tests that calling install multiple times does not alter existing WebSocket support on the app.
    
    Verifies that repeated installation leaves the connection manager and route registration method unchanged, ensuring idempotency.
    """
    app = DummyApp()
    install(app)  # type: ignore[arg-type]
    app_any = cast(SupportsWebSocket, app)
    first_manager = app_any.ws_connection_manager
    first_route_fn = app_any.add_websocket_route

    install(app)  # type: ignore[arg-type]
    assert app_any.ws_connection_manager is first_manager
    assert app_any.add_websocket_route is first_route_fn


def test_install_detects_partial_state() -> None:
    """
    Tests that `install` raises a RuntimeError if called on an app missing internal WebSocket state.
    
    Simulates a partially installed or tampered app by deleting the `_websocket_routes` attribute after installation, then verifies that a subsequent call to `install` detects the inconsistency and raises a RuntimeError.
    """
    app = DummyApp()
    install(app)  # type: ignore[arg-type]
    app_any = cast(SupportsWebSocket, app)

    # Simulate tampering with one of the install attributes
    delattr(app_any, "_websocket_routes")

    with pytest.raises(RuntimeError):
        install(app)  # type: ignore[arg-type]


def test_add_websocket_route_duplicate_raises() -> None:
    app = DummyApp()
    install(app)  # type: ignore[arg-type]
    app_any = cast(SupportsWebSocket, app)

    resource = object()
    app_any.add_websocket_route("/ws", resource)

    with pytest.raises(ValueError):
        app_any.add_websocket_route("/ws", resource)
