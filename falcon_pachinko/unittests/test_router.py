"""Tests for the WebSocketRouter class."""

from __future__ import annotations

import inspect
import typing

import pytest

from falcon_pachinko import WebSocketResource, WebSocketRouter

pytest_plugins = ["falcon_pachinko.unittests.test_app_install"]

if typing.TYPE_CHECKING:
    from falcon_pachinko.unittests.test_app_install import SupportsWebSocket


class DummyResource(WebSocketResource):
    """Capture connection parameters for testing."""

    async def on_connect(self, req: object, ws: object, **params: object) -> bool:
        """Record params and refuse the connection."""
        self.params = params
        return False


def test_router_is_resource() -> None:
    """Verify the router exposes a valid ``on_websocket`` responder."""
    router = WebSocketRouter()
    assert inspect.iscoroutinefunction(router.on_websocket)


def test_deprecation_warnings(
    dummy_app: SupportsWebSocket,
    dummy_resource_cls: type[WebSocketResource],
) -> None:
    """Ensure legacy APIs emit :class:`DeprecationWarning`."""
    with pytest.deprecated_call():
        dummy_app.add_websocket_route("/ws", dummy_resource_cls)

    dummy_app.add_websocket_route("/ws2", dummy_resource_cls)
    with pytest.deprecated_call():
        dummy_app.create_websocket_resource("/ws2")
