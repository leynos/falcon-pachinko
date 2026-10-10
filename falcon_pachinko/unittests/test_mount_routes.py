"""Tests for mounted route matching and parameter registration."""

from __future__ import annotations

import typing as typ

import falcon
import pytest

from falcon_pachinko import (
    HookCollection,
    HookContext,
    WebSocketResource,
    WebSocketRouter,
)
from falcon_pachinko.unittests.helpers import DummyWS, make_req


class _ParameterizedResource(WebSocketResource):
    """Capture the parameters supplied by a mounted route."""

    instances: typ.ClassVar[list[_ParameterizedResource]] = []

    def __init__(self) -> None:
        self.params: dict[str, object] = {}
        self.instances.append(self)

    async def on_connect(self, req: object, ws: object, **params: object) -> bool:
        self.params = params
        return False


class _StatusResource(WebSocketResource):
    """Record construction and hook activity for one mounted status route."""

    def __init__(self, events: list[str]) -> None:
        self.events = events
        events.append("factory")

    async def on_connect(self, req: object, ws: object, **params: object) -> bool:
        return False


_StatusResource.hooks = HookCollection()


def _record_status_hook(context: HookContext) -> None:
    resource = typ.cast("_StatusResource", context.target)
    resource.events.append("hook")


_StatusResource.hooks.add("before_connect", _record_status_hook)


@pytest.mark.asyncio
async def test_metacharacter_mount_prefix_matches_literal_text() -> None:
    """A regex-expanded mount prefix does not instantiate a resource."""
    events: list[str] = []
    router = WebSocketRouter()
    router.add_route("/status", _StatusResource, events)
    router.mount("/api.v1")

    with pytest.raises(falcon.HTTPNotFound):
        await router.on_websocket(make_req("/api/v1/status", "/api.v1"), DummyWS())
    assert not events, "a regex-expanded mount prefix must not create a resource"

    await router.on_websocket(make_req("/api.v1/status", "/api.v1"), DummyWS())
    assert events == ["factory", "hook"], "the literal mount prefix should dispatch"


def test_mount_parameter_collision_does_not_register_route() -> None:
    """A mount-prefix name conflict leaves route registration untouched."""
    router = WebSocketRouter()
    router.mount("/ws/{room}")

    with pytest.raises(ValueError, match="Duplicate parameter name 'room'"):
        router.add_route("/chat/{room}", _ParameterizedResource, name="chat")

    with pytest.raises(KeyError, match="no route registered with name 'chat'"):
        router.url_for("chat", room="general")

    router.add_route("/chat/{channel}", _ParameterizedResource, name="chat")
    assert router.url_for("chat", channel="general") == "/chat/general", (
        "a corrected registration should reuse the rejected name"
    )


@pytest.mark.asyncio
async def test_mount_parameter_collision_leaves_routes_usable() -> None:
    """A composed-template error does not commit partial mount state."""
    _ParameterizedResource.instances.clear()
    router = WebSocketRouter()
    router.add_route("/health", _ParameterizedResource)
    router.add_route("/chat/{room}", _ParameterizedResource)

    with pytest.raises(ValueError, match="Duplicate parameter name 'room'"):
        router.mount("/ws/{room}")

    router.mount("/ws/{scope}")
    await router.on_websocket(make_req("/ws/acme/health", "/ws/{scope}"), DummyWS())
    assert _ParameterizedResource.instances[-1].params == {"scope": "acme"}, (
        "the first stored route should work after a corrected mount"
    )

    await router.on_websocket(
        make_req("/ws/acme/chat/general", "/ws/{scope}"), DummyWS()
    )
    assert _ParameterizedResource.instances[-1].params == {
        "scope": "acme",
        "room": "general",
    }, "all stored routes should work after a corrected mount"
