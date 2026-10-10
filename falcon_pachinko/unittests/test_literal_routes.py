"""Tests for literal top-level route and mount matching."""

from __future__ import annotations

import falcon
import pytest

from falcon_pachinko import HookCollection, WebSocketResource, WebSocketRouter
from falcon_pachinko.unittests.helpers import DummyWS, make_req


@pytest.mark.parametrize("route_order", ["dotted-first", "slashed-first"])
@pytest.mark.asyncio
async def test_literal_top_level_routes_select_resources_and_hooks(
    route_order: str,
) -> None:
    """Competing literal routes retain their own resources and hooks."""
    events: list[str] = []
    selected: list[str] = []

    class DottedResource(WebSocketResource):
        def __init__(self) -> None:
            events.append("dotted.factory")

        async def on_connect(self, req: object, ws: object, **params: object) -> bool:
            selected.append("dotted")
            return False

    class SlashedResource(WebSocketResource):
        def __init__(self) -> None:
            events.append("slashed.factory")

        async def on_connect(self, req: object, ws: object, **params: object) -> bool:
            selected.append("slashed")
            return False

    DottedResource.hooks = HookCollection()
    SlashedResource.hooks = HookCollection()
    DottedResource.hooks.add(
        "before_connect", lambda context: events.append("dotted.hook")
    )
    SlashedResource.hooks.add(
        "before_connect", lambda context: events.append("slashed.hook")
    )

    router = WebSocketRouter()
    routes = [
        ("/child.v1", DottedResource),
        ("/child/v1", SlashedResource),
    ]
    if route_order == "slashed-first":
        routes.reverse()
    for path, resource in routes:
        router.add_route(path, resource)
    router.mount("/")

    await router.on_websocket(make_req("/child.v1"), DummyWS())
    assert selected == ["dotted"], "the literal dotted route should select its resource"
    assert events == ["dotted.factory", "dotted.hook"], (
        "the dotted route should run only its resource-specific hook"
    )

    selected.clear()
    events.clear()
    await router.on_websocket(make_req("/child/v1"), DummyWS())
    assert selected == ["slashed"], "the slash route should select its resource"
    assert events == ["slashed.factory", "slashed.hook"], (
        "the slash route should run only its resource-specific hook"
    )

    selected.clear()
    events.clear()
    with pytest.raises(falcon.HTTPNotFound):
        await router.on_websocket(make_req("/childxv1"), DummyWS())
    assert not selected, "a near-miss should not select a resource"
    assert not events, "a near-miss should not run resource hooks"
