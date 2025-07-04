"""Tests for the WebSocketRouter class."""

from __future__ import annotations

import inspect
import typing

import falcon
import pytest

from falcon_pachinko import WebSocketResource, WebSocketRouter
from falcon_pachinko.unittests.helpers import DummyWS

pytest_plugins = ["falcon_pachinko.unittests.test_app_install"]

if typing.TYPE_CHECKING:
    from falcon_pachinko.unittests.test_app_install import SupportsWebSocket


class DummyResource(WebSocketResource):
    """Capture connection parameters for testing."""

    instances: typing.ClassVar[list[DummyResource]] = []

    def __init__(self) -> None:  # pragma: no cover - simple init
        DummyResource.instances.append(self)

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


@pytest.mark.asyncio
async def test_parameterized_route_and_url_for() -> None:
    """Verify parameter matching and URL reversal."""
    DummyResource.instances.clear()
    router = WebSocketRouter()
    router.add_route("/rooms/{room}", DummyResource, name="room")

    assert router.url_for("room", room="abc") == "/rooms/abc"

    req = type("Req", (), {"path": "/api/rooms/42", "path_template": "/api"})()
    await router.on_websocket(req, DummyWS())

    assert DummyResource.instances[-1].params == {"room": "42"}


@pytest.mark.asyncio
async def test_not_found_raises() -> None:
    """Ensure unmatched paths raise HTTPNotFound."""
    router = WebSocketRouter()
    router.add_route("/ok", DummyResource)
    req = type("Req", (), {"path": "/missing"})()

    with pytest.raises(falcon.HTTPNotFound):
        await router.on_websocket(req, DummyWS())


def test_add_route_requires_callable() -> None:
    """Non-callable resources must raise ``TypeError``."""
    router = WebSocketRouter()
    bad_resource = typing.cast("typing.Any", object())
    with pytest.raises(TypeError):
        router.add_route("/x", bad_resource)


def test_url_for_unknown_route() -> None:
    """Missing route names should raise a descriptive ``KeyError``."""
    router = WebSocketRouter()
    router.add_route("/x", DummyResource, name="x")

    with pytest.raises(KeyError, match="no route registered"):
        router.url_for("missing")
