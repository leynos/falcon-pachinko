"""Tests for nested resource composition."""

import typing
from types import SimpleNamespace

import falcon
import pytest

from falcon_pachinko import WebSocketResource, WebSocketRouter
from falcon_pachinko.unittests.helpers import DummyWS


class Child(WebSocketResource):
    """Capture parameters passed to ``on_connect``."""

    instances: typing.ClassVar[list["Child"]] = []

    def __init__(self) -> None:
        """Record instance creation."""
        Child.instances.append(self)

    async def on_connect(self, req: object, ws: object, **params: object) -> bool:
        """Store connection params."""
        self.params = params
        return False


class Parent(WebSocketResource):
    """Parent resource with nested subroute."""

    def __init__(self) -> None:
        """Register subroute."""
        self.add_subroute("child/{cid}", Child)

    async def on_connect(self, req: object, ws: object, **params: object) -> bool:
        """Store parameters and refuse the connection."""
        self.params = params
        return False


@pytest.mark.asyncio
async def test_nested_subroute_params() -> None:
    """Parameters from each route level are merged."""
    Child.instances.clear()
    router = WebSocketRouter()
    router.add_route("/parent/{pid}", Parent)
    router.mount("/")
    req = typing.cast(
        "falcon.Request",
        SimpleNamespace(path="/parent/1/child/2", path_template=""),
    )
    await router.on_websocket(req, DummyWS())

    assert Child.instances[-1].params == {"pid": "1", "cid": "2"}


@pytest.mark.asyncio
async def test_nested_subroute_not_found() -> None:
    """Unmatched nested path should raise HTTPNotFound."""
    router = WebSocketRouter()
    router.add_route("/parent/{pid}", Parent)
    router.mount("/")
    req = typing.cast(
        "falcon.Request",
        SimpleNamespace(path="/parent/1/oops", path_template=""),
    )
    with pytest.raises(falcon.HTTPNotFound):
        await router.on_websocket(req, DummyWS())


@pytest.mark.asyncio
async def test_nested_subroute_malformed_path() -> None:
    """Missing slash between segments should not match."""
    router = WebSocketRouter()
    router.add_route("/parent/{pid}", Parent)
    router.mount("/")
    req = typing.cast(
        "falcon.Request",
        SimpleNamespace(path="/parent/1child/2", path_template=""),
    )
    with pytest.raises(falcon.HTTPNotFound):
        await router.on_websocket(req, DummyWS())


def test_add_subroute_invalid_resource() -> None:
    """add_subroute must reject non-callables."""
    r = WebSocketResource()
    with pytest.raises(TypeError):
        r.add_subroute("child", typing.cast("typing.Any", object()))
