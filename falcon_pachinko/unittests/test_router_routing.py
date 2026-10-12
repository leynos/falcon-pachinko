"""Tests for WebSocketRouter route registration and matching."""

from __future__ import annotations

import typing as typ
from types import SimpleNamespace

import falcon
import pytest

from falcon_pachinko import WebSocketResource, WebSocketRouter
from falcon_pachinko.unittests._router_test_resources import (
    AcceptingResource,
    DummyResource,
)
from falcon_pachinko.unittests.helpers import DummyWS, RecordingWS, make_req


@pytest.mark.asyncio
async def test_parameterized_route_and_url_for() -> None:
    """Verify parameter matching and URL reversal."""
    DummyResource.instances.clear()
    router = WebSocketRouter()
    router.add_route("/rooms/{room}", DummyResource, name="room")
    router.mount("/api")

    # Test non-trailing slash
    assert router.url_for("room", room="abc") == "/rooms/abc", (
        "url_for should reverse the route template with the given param"
    )
    req = make_req("/api/rooms/42", "/api")
    await router.on_websocket(req, DummyWS())
    assert DummyResource.instances[-1].params == {"room": "42"}, (
        "matched route should pass the path param to on_connect"
    )

    with pytest.raises(KeyError) as excinfo:
        router.url_for("room")
    assert "room" in str(excinfo.value), "error should name the missing param"


@pytest.mark.asyncio
async def test_trailing_and_nontrailing_slash_routes() -> None:
    """Test route matching and url_for with trailing and non-trailing slashes."""
    DummyResource.instances.clear()
    router = WebSocketRouter()
    router.add_route("/rooms/{room}/", DummyResource, name="room_trailing")
    router.add_route("/rooms2/{room}", DummyResource, name="room_nontrailing")
    router.mount("/")

    # Trailing slash
    assert router.url_for("room_trailing", room="xyz") == "/rooms/xyz/", (
        "url_for should preserve the trailing slash from the route template"
    )
    req_trailing = make_req("/rooms/123/")
    await router.on_websocket(req_trailing, DummyWS())
    assert DummyResource.instances[-1].params == {"room": "123"}, (
        "trailing-slash route should match and extract the param"
    )

    # Non-trailing slash
    assert router.url_for("room_nontrailing", room="uvw") == "/rooms2/uvw", (
        "url_for should not add a trailing slash absent from the route template"
    )
    req_non = make_req("/rooms2/456")
    await router.on_websocket(req_non, DummyWS())
    assert DummyResource.instances[-1].params == {"room": "456"}, (
        "non-trailing-slash route should match and extract the param"
    )


@pytest.mark.asyncio
async def test_not_found_raises() -> None:
    """Ensure unmatched paths raise HTTPNotFound."""
    router = WebSocketRouter()
    router.add_route("/ok", DummyResource)
    router.mount("/")
    req = make_req("/missing")

    with pytest.raises(falcon.HTTPNotFound):
        await router.on_websocket(req, DummyWS())


@pytest.mark.asyncio
async def test_path_template_prefix_mismatch() -> None:
    """Mismatch between ``path_template`` and request should return 404."""
    router = WebSocketRouter()
    router.add_route("/rooms/{room}", DummyResource)
    router.mount("/")
    req = make_req("/rooms/1", "/api")

    with pytest.raises(falcon.HTTPNotFound):
        await router.on_websocket(req, DummyWS())


@pytest.mark.asyncio
async def test_none_path_templates_default_to_root_mount() -> None:
    """A present-but-None Falcon template still defaults to the root mount."""
    router = WebSocketRouter()
    router.add_route("/rooms/{room}", DummyResource)
    router.mount("/")
    req = SimpleNamespace(path="/rooms/1", path_template=None, uri_template=None)

    await router.on_websocket(req, DummyWS())


def test_add_route_requires_callable() -> None:
    """Non-callable resources must raise ``TypeError``."""
    router = WebSocketRouter()
    # The cast smuggles a deliberately non-callable value past the signature
    # to exercise the runtime type check.
    bad_resource = typ.cast("typ.Any", object())
    with pytest.raises(TypeError):
        router.add_route("/x", bad_resource)


def test_add_route_duplicate_name_and_path() -> None:
    """Duplicate names or paths should raise ``ValueError``."""
    router = WebSocketRouter()
    router.add_route("/a", DummyResource, name="dup")
    with pytest.raises(ValueError, match="already registered"):
        router.add_route("/b", DummyResource, name="dup")

    with pytest.raises(ValueError, match="already registered"):
        router.add_route("/a/", DummyResource)


def test_add_route_duplicates_after_mount() -> None:
    """Adding duplicates after mounting should raise ``ValueError``."""
    router = WebSocketRouter()
    router.add_route("/dup", DummyResource, name="dup")
    router.mount("/api")

    with pytest.raises(ValueError, match="already registered"):
        router.add_route("/dup", DummyResource, name="other")

    with pytest.raises(ValueError, match="already registered"):
        router.add_route("/other", DummyResource, name="dup")


def test_add_route_invalid_template() -> None:
    """Empty parameter names should raise ``ValueError``."""
    router = WebSocketRouter()
    with pytest.raises(ValueError, match="Empty parameter name"):
        router.add_route("/rooms/{}", DummyResource)


@pytest.mark.asyncio
async def test_mount_compiles_existing_and_new_routes() -> None:
    """Routes defined before and after mount should work."""
    DummyResource.instances.clear()
    router = WebSocketRouter()
    router.add_route("/before/{id}", DummyResource)
    router.mount("/api")
    router.add_route("/after/{id}", DummyResource)

    req_before = make_req("/api/before/1", "/api")
    await router.on_websocket(req_before, DummyWS())
    assert DummyResource.instances[-1].params == {"id": "1"}, (
        "route added before mount should match"
    )

    req_after = make_req("/api/after/2", "/api")
    await router.on_websocket(req_after, DummyWS())
    assert DummyResource.instances[-1].params == {"id": "2"}, (
        "route added after mount should also match"
    )


def test_mount_twice_raises_error() -> None:
    """Attempting to mount twice should raise RuntimeError."""
    router = WebSocketRouter()
    router.mount("/api")
    with pytest.raises(RuntimeError, match="already mounted"):
        router.mount("/v2")


@pytest.mark.asyncio
async def test_mount_with_empty_vs_slash_prefix() -> None:
    """Validate behaviour between empty and slash prefixes."""
    router_slash = WebSocketRouter()
    router_slash.add_route("/x", AcceptingResource)
    router_slash.mount("/")
    ws_slash = RecordingWS()
    await router_slash.on_websocket(make_req("/x", "/"), ws_slash)
    assert ws_slash.accepted, "a router mounted at '/' should accept the connection"

    router_empty = WebSocketRouter()
    router_empty.add_route("/y", AcceptingResource)
    router_empty.mount("")
    ws_empty = RecordingWS()
    await router_empty.on_websocket(make_req("/y"), ws_empty)
    assert ws_empty.accepted, "a router mounted at '' should accept the connection"


@pytest.mark.asyncio
async def test_overlapping_routes() -> None:
    """Ensure the first matching route is used when paths overlap."""

    class First(WebSocketResource):
        instances: typ.ClassVar[list[First]] = []

        def __init__(self) -> None:
            First.instances.append(self)

        async def on_connect(self, req: object, ws: object, **params: object) -> bool:
            return False

    class Second(WebSocketResource):
        instances: typ.ClassVar[list[Second]] = []

        def __init__(self) -> None:
            Second.instances.append(self)

        async def on_connect(self, req: object, ws: object, **params: object) -> bool:
            return False

    router = WebSocketRouter()
    router.add_route("/over/{id}", First)
    router.add_route("/over/static", Second)
    router.mount("/")

    req = make_req("/over/static")
    await router.on_websocket(req, DummyWS())

    assert First.instances, "the earlier registered route should match first"
    assert not Second.instances, "the overlapping later route should not be reached"


def test_url_for_unknown_route() -> None:
    """Missing route names should raise a descriptive ``KeyError``."""
    router = WebSocketRouter()
    router.add_route("/x", DummyResource, name="x")
    router.mount("/")

    with pytest.raises(KeyError, match="no route registered"):
        router.url_for("missing")


def test_url_for_missing_params() -> None:
    """Missing params should raise ``KeyError`` with the param name."""
    router = WebSocketRouter()
    router.add_route("/rooms/{room}", DummyResource, name="room")
    router.mount("/")

    with pytest.raises(KeyError) as excinfo:
        router.url_for("room")
    assert "room" in str(excinfo.value), "error should name the missing param"
