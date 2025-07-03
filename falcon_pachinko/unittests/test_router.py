"""Tests for the :mod:`falcon_pachinko.router` module."""

from __future__ import annotations

import types
import typing

import pytest

from falcon_pachinko import (
    WebSocketLike,
    WebSocketResource,
    WebSocketRouter,
)
from falcon_pachinko.router import DuplicateRouteNameError, WebSocketRouteNotFoundError

if typing.TYPE_CHECKING:  # pragma: no cover - imports for type hints only
    import falcon
from falcon_pachinko.unittests.helpers import DummyWS


class DummyResource(WebSocketResource):
    """A simple resource used in router tests."""

    def __init__(self, value: int) -> None:
        self.value = value
        self.params: dict[str, str] | None = None

    async def on_connect(
        self, req: falcon.Request, ws: WebSocketLike, **params: str
    ) -> bool:
        """Record connection parameters for assertions."""
        self.params = params
        return True


@pytest.mark.asyncio
async def test_router_dispatches_to_route() -> None:
    """Verify that ``on_websocket`` instantiates the correct resource."""
    router = WebSocketRouter()
    router.add_route("/{id}", DummyResource, init_args=(1,), name="item")

    req = typing.cast(
        "falcon.Request", types.SimpleNamespace(path="/ws/42", uri_template="/ws")
    )
    ws = DummyWS()
    resource = await router.on_websocket(req, ws)

    assert isinstance(resource, DummyResource)
    assert resource.value == 1
    assert resource.params == {"id": "42"}


def test_url_for_generates_path() -> None:
    """Ensure ``url_for`` fills in path parameters."""
    router = WebSocketRouter(name="r")
    router.add_route("/things/{tid}", DummyResource, name="thing")

    path = router.url_for("thing", tid="99")
    assert path == "/things/99"


def test_duplicate_route_name_raises() -> None:
    """``add_route`` should reject duplicate names."""
    router = WebSocketRouter()
    router.add_route("/a", DummyResource, name="dup")

    with pytest.raises(DuplicateRouteNameError):
        router.add_route("/b", DummyResource, name="dup")


@pytest.mark.asyncio
async def test_route_not_found() -> None:
    """``on_websocket`` should raise for unknown paths."""
    router = WebSocketRouter()
    req = typing.cast(
        "falcon.Request", types.SimpleNamespace(path="/missing", uri_template="/ws")
    )

    with pytest.raises(WebSocketRouteNotFoundError):
        await router.on_websocket(req, DummyWS())


@pytest.mark.asyncio
async def test_callable_target_supported() -> None:
    """The router should handle callables as targets."""

    def factory() -> WebSocketResource:
        return DummyResource(2)

    router = WebSocketRouter()
    router.add_route("/", factory, name="home")

    req = typing.cast(
        "falcon.Request", types.SimpleNamespace(path="/ws/", uri_template="/ws")
    )
    res = await router.on_websocket(req, DummyWS())
    assert isinstance(res, DummyResource)
    assert res.value == 2
