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


@pytest.fixture
def dummy_app() -> SupportsWebSocket:
    app = DummyApp()
    install(app)  # type: ignore[arg-type]
    return typing.cast("SupportsWebSocket", app)


@pytest.fixture
def dummy_resource_cls(dummy_app: SupportsWebSocket) -> type[WebSocketResource]:
    class DummyResource(WebSocketResource):
        pass

    return DummyResource


def test_install_adds_methods_and_manager(dummy_app: SupportsWebSocket) -> None:
    """Ensure ``install()`` attaches WebSocket helpers to the app."""

    app_any = dummy_app

    assert hasattr(app_any, "ws_connection_manager")
    assert isinstance(app_any.ws_connection_manager, WebSocketConnectionManager)
    assert callable(app_any.add_websocket_route)
    assert callable(app_any.create_websocket_resource)


def test_add_websocket_route_registers_resource(
    dummy_app: SupportsWebSocket, dummy_resource_cls: type[WebSocketResource]
) -> None:
    """``add_websocket_route`` stores the resource class for the path."""
    dummy_app.add_websocket_route("/ws", dummy_resource_cls)

    assert (
        dummy_app._websocket_routes["/ws"]  # pyright: ignore[reportPrivateUsage]
        is dummy_resource_cls
    )


def test_install_is_idempotent(dummy_app: SupportsWebSocket) -> None:
    """Calling ``install()`` twice leaves existing state intact."""
    first_manager = dummy_app.ws_connection_manager
    first_route_fn = dummy_app.add_websocket_route
    first_create_fn = dummy_app.create_websocket_resource

    install(dummy_app)  # type: ignore[arg-type]
    assert dummy_app.ws_connection_manager is first_manager
    assert dummy_app.add_websocket_route is first_route_fn
    assert dummy_app.create_websocket_resource is first_create_fn


def test_install_detects_partial_state(dummy_app: SupportsWebSocket) -> None:
    """``install()`` raises if previous install state is corrupted."""

    # Simulate tampering with one of the install attributes
    delattr(dummy_app, "_websocket_routes")
    delattr(dummy_app, "create_websocket_resource")

    with pytest.raises(RuntimeError):
        install(dummy_app)  # type: ignore[arg-type]


def test_add_websocket_route_duplicate_raises(
    dummy_app: SupportsWebSocket, dummy_resource_cls: type[WebSocketResource]
) -> None:
    dummy_app.add_websocket_route("/ws", dummy_resource_cls)

    with pytest.raises(ValueError):
        dummy_app.add_websocket_route("/ws", dummy_resource_cls)


@pytest.mark.parametrize(
    "path",
    ["ws", "", " /ws", "/ws ", 123],
)
def test_add_websocket_route_invalid_path(
    dummy_app: SupportsWebSocket,
    dummy_resource_cls: type[WebSocketResource],
    path: object,
) -> None:
    with pytest.raises(ValueError):
        dummy_app.add_websocket_route(path, dummy_resource_cls)  # type: ignore[arg-type]


def test_create_websocket_resource_returns_new_instances(
    dummy_app: SupportsWebSocket, dummy_resource_cls: type[WebSocketResource]
) -> None:
    dummy_app.add_websocket_route("/ws", dummy_resource_cls)

    first = dummy_app.create_websocket_resource("/ws")
    second = dummy_app.create_websocket_resource("/ws")

    assert isinstance(first, dummy_resource_cls)
    assert isinstance(second, dummy_resource_cls)
    assert first is not second


def test_create_websocket_resource_unregistered_path(
    dummy_app: SupportsWebSocket,
) -> None:
    with pytest.raises(ValueError):
        dummy_app.create_websocket_resource("/missing")


def test_add_websocket_route_type_check(dummy_app: SupportsWebSocket) -> None:
    with pytest.raises(TypeError):
        dummy_app.add_websocket_route("/ws", object)  # type: ignore[arg-type]
