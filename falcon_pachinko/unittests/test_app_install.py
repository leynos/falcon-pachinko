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
        """Return a new resource instance for the given WebSocket path."""
        ...

    def add_websocket_route(self, path: str, resource: type[object]) -> None:
        """Register a ``WebSocketResource`` subclass for ``path``."""
        ...


def test_install_adds_methods_and_manager() -> None:
    """Ensure ``install()`` attaches WebSocket helpers to the app."""
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
    """Calling ``install()`` twice leaves existing state intact."""
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
    """``install()`` raises if previous install state is corrupted."""
    app = DummyApp()
    install(app)  # type: ignore[arg-type]
    app_any = typing.cast("SupportsWebSocket", app)

    # Simulate tampering with one of the install attributes
    delattr(app_any, "_websocket_routes")
    delattr(app_any, "create_websocket_resource")

    with pytest.raises(RuntimeError):
        install(app)  # type: ignore[arg-type]


def test_add_websocket_route_duplicate_raises() -> None:
    app = DummyApp()
    install(app)  # type: ignore[arg-type]
    app_any = typing.cast("SupportsWebSocket", app)

    class R(WebSocketResource):
        pass

    app_any.add_websocket_route("/ws", R)

    with pytest.raises(ValueError):
        app_any.add_websocket_route("/ws", R)


def test_create_websocket_resource_returns_new_instances() -> None:
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


def test_create_websocket_resource_unregistered_path() -> None:
    app = DummyApp()
    install(app)  # type: ignore[arg-type]
    app_any = typing.cast("SupportsWebSocket", app)

    with pytest.raises(ValueError):
        app_any.create_websocket_resource("/missing")


def test_add_websocket_route_type_check() -> None:
    app = DummyApp()
    install(app)  # type: ignore[arg-type]
    app_any = typing.cast("SupportsWebSocket", app)

    with pytest.raises(TypeError):
        app_any.add_websocket_route("/ws", object)  # type: ignore[arg-type]
