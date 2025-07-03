"""Tests for the :mod:`falcon_pachinko.router` module."""

from __future__ import annotations

import types

import pytest

from falcon_pachinko import WebSocketLike, WebSocketResource, WebSocketRouter
from falcon_pachinko.unittests.helpers import DummyWS


class DummyResource(WebSocketResource):
    """A simple resource used in router tests."""

    def __init__(self, value: int) -> None:
        self.value = value
        self.params: dict[str, str] | None = None

    async def on_connect(
        self, req: types.SimpleNamespace, ws: WebSocketLike, **params: str
    ) -> bool:
        """Record connection parameters for assertions."""
        self.params = params
        return True


@pytest.mark.asyncio
async def test_router_dispatches_to_route() -> None:
    """Verify that ``on_websocket`` instantiates the correct resource."""
    router = WebSocketRouter()
    router.add_route("/{id}", DummyResource, init_args=(1,), name="item")

    req = types.SimpleNamespace(path="/ws/42", uri_template="/ws")
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
