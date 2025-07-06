"""Tests for the WebSocketRouter class."""

from __future__ import annotations

import inspect
import typing

import falcon
import falcon.asgi
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


class AcceptingResource(WebSocketResource):
    """Resource that always accepts the connection."""

    async def on_connect(self, req: object, ws: object, **params: object) -> bool:
        """Signal that the connection should be accepted."""
        return True


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
    router.mount("/api")

    # Test non-trailing slash
    assert router.url_for("room", room="abc") == "/rooms/abc"
    req = type("Req", (), {"path": "/api/rooms/42", "path_template": "/api"})()
    await router.on_websocket(req, DummyWS())
    assert DummyResource.instances[-1].params == {"room": "42"}

    with pytest.raises(KeyError) as excinfo:
        router.url_for("room")
    assert "room" in str(excinfo.value)


@pytest.mark.asyncio
async def test_trailing_and_nontrailing_slash_routes() -> None:
    """Test route matching and url_for with trailing and non-trailing slashes."""
    DummyResource.instances.clear()
    router = WebSocketRouter()
    router.add_route("/rooms/{room}/", DummyResource, name="room_trailing")
    router.add_route("/rooms2/{room}", DummyResource, name="room_nontrailing")
    router.mount("/")

    # Trailing slash
    assert router.url_for("room_trailing", room="xyz") == "/rooms/xyz/"
    req_trailing = type("Req", (), {"path": "/rooms/123/", "path_template": ""})()
    await router.on_websocket(req_trailing, DummyWS())
    assert DummyResource.instances[-1].params == {"room": "123"}

    # Non-trailing slash
    assert router.url_for("room_nontrailing", room="uvw") == "/rooms2/uvw"
    req_non = type("Req", (), {"path": "/rooms2/456", "path_template": ""})()
    await router.on_websocket(req_non, DummyWS())
    assert DummyResource.instances[-1].params == {"room": "456"}


@pytest.mark.asyncio
async def test_not_found_raises() -> None:
    """Ensure unmatched paths raise HTTPNotFound."""
    router = WebSocketRouter()
    router.add_route("/ok", DummyResource)
    router.mount("/")
    req = type("Req", (), {"path": "/missing"})()

    with pytest.raises(falcon.HTTPNotFound):
        await router.on_websocket(req, DummyWS())


@pytest.mark.asyncio
async def test_path_template_prefix_mismatch() -> None:
    """Mismatch between ``path_template`` and request should return 404."""
    router = WebSocketRouter()
    router.add_route("/rooms/{room}", DummyResource)
    router.mount("/")
    req = type("Req", (), {"path": "/rooms/1", "path_template": "/api"})()

    with pytest.raises(falcon.HTTPNotFound):
        await router.on_websocket(req, DummyWS())


@pytest.mark.asyncio
async def test_on_connect_accepts_connection() -> None:
    """Ensure ws.accept() is called when on_connect returns True."""
    router = WebSocketRouter()
    router.add_route("/ok", AcceptingResource)
    router.mount("/")
    ws = DummyWS()
    called = {}

    async def accept() -> None:
        called["accepted"] = True

    typing.cast("typing.Any", ws).accept = accept
    req = type("Req", (), {"path": "/ok"})()
    await router.on_websocket(req, ws)
    assert called.get("accepted") is True


def test_add_route_requires_callable() -> None:
    """Non-callable resources must raise ``TypeError``."""
    router = WebSocketRouter()
    bad_resource = typing.cast("typing.Any", object())
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

    req_before = type("Req", (), {"path": "/api/before/1", "path_template": "/api"})()
    await router.on_websocket(req_before, DummyWS())
    assert DummyResource.instances[-1].params == {"id": "1"}

    req_after = type("Req", (), {"path": "/api/after/2", "path_template": "/api"})()
    await router.on_websocket(req_after, DummyWS())
    assert DummyResource.instances[-1].params == {"id": "2"}


def test_mount_twice_raises_error() -> None:
    """Attempting to mount twice should raise RuntimeError."""
    router = WebSocketRouter()
    router.mount("/api")
    with pytest.raises(RuntimeError, match="already mounted"):
        router.mount("/v2")


@pytest.mark.asyncio
async def test_mount_with_empty_vs_slash_prefix() -> None:
    """Validate behavior between empty and slash prefixes."""
    router_slash = WebSocketRouter()
    router_slash.add_route("/x", AcceptingResource)
    router_slash.mount("/")
    req = type("Req", (), {"path": "/x", "path_template": "/"})()
    await router_slash.on_websocket(req, DummyWS())

    router_empty = WebSocketRouter()
    router_empty.add_route("/y", AcceptingResource)
    router_empty.mount("")
    req_empty = type("Req", (), {"path": "/y", "path_template": ""})()
    await router_empty.on_websocket(req_empty, DummyWS())


@pytest.mark.asyncio
async def test_overlapping_routes() -> None:
    """Ensure the first matching route is used when paths overlap."""

    class First(WebSocketResource):
        instances: typing.ClassVar[list[First]] = []

        def __init__(self) -> None:
            First.instances.append(self)

        async def on_connect(self, req: object, ws: object, **params: object) -> bool:
            return False

    class Second(WebSocketResource):
        instances: typing.ClassVar[list[Second]] = []

        def __init__(self) -> None:
            Second.instances.append(self)

        async def on_connect(self, req: object, ws: object, **params: object) -> bool:
            return False

    router = WebSocketRouter()
    router.add_route("/over/{id}", First)
    router.add_route("/over/static", Second)
    router.mount("/")

    req = type("Req", (), {"path": "/over/static", "path_template": ""})()
    await router.on_websocket(req, DummyWS())

    assert First.instances
    assert not Second.instances


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
    assert "room" in str(excinfo.value)


@pytest.mark.asyncio
async def test_on_connect_exception_closes_ws() -> None:
    """Exceptions in ``on_connect`` should close the connection."""

    class BadResource(WebSocketResource):
        async def on_connect(self, req: object, ws: object, **params: object) -> bool:
            raise RuntimeError("boom")

    router = WebSocketRouter()
    router.add_route("/boom", BadResource)
    router.mount("/")
    ws = DummyWS()
    called = {}

    async def close(code: int = 1000) -> None:  # pragma: no cover - simple stub
        called["closed"] = code

    typing.cast("typing.Any", ws).close = close

    req = type("Req", (), {"path": "/boom"})()
    with pytest.raises(RuntimeError):
        await router.on_websocket(req, ws)

    assert called.get("closed") == 1000


@pytest.mark.asyncio
async def test_resource_init_args_kwargs() -> None:
    """Ensure ``add_route`` forwards init args and kwargs."""

    class ParamResource(WebSocketResource):
        instances: typing.ClassVar[list[ParamResource]] = []

        def __init__(self, foo: str, *, bar: int) -> None:
            self.foo = foo
            self.bar = bar
            ParamResource.instances.append(self)

        async def on_connect(self, req: object, ws: object, **params: object) -> bool:
            self.params = params
            return False

    router = WebSocketRouter()
    router.add_route(
        "/p/{id}", ParamResource, args=("hey",), kwargs={"bar": 5}, name="p"
    )
    router.mount("/")

    req = type("Req", (), {"path": "/p/1", "path_template": ""})()
    await router.on_websocket(req, DummyWS())
    inst = ParamResource.instances[-1]
    assert inst.foo == "hey"
    assert inst.bar == 5


@pytest.mark.asyncio
async def test_resource_missing_init_args() -> None:
    """Invalid or missing init args should raise ``TypeError`` and close the WS."""

    class NeedsArgs(WebSocketResource):
        def __init__(self, value: int) -> None:
            self.value = value

        async def on_connect(self, req: object, ws: object, **params: object) -> bool:
            return False

    router = WebSocketRouter()
    router.add_route("/w", NeedsArgs)
    router.mount("/")
    ws = DummyWS()
    closed: dict[str, object] = {}

    async def close(code: int = 1000) -> None:  # pragma: no cover - simple stub
        closed["closed"] = code

    typing.cast("typing.Any", ws).close = close
    req = type("Req", (), {"path": "/w", "path_template": ""})()

    with pytest.raises(TypeError):
        await router.on_websocket(req, ws)

    assert closed.get("closed") == 1000


@pytest.mark.asyncio
async def test_resource_unexpected_init_kwargs() -> None:
    """Unexpected kwargs should raise ``TypeError`` and close the WS."""

    class NoKwargs(WebSocketResource):
        async def on_connect(self, req: object, ws: object, **params: object) -> bool:
            return False

    router = WebSocketRouter()
    router.add_route("/x", NoKwargs, kwargs={"oops": 1})
    router.mount("/")
    ws = DummyWS()
    closed: dict[str, object] = {}

    async def close(code: int = 1000) -> None:  # pragma: no cover - simple stub
        closed["closed"] = code

    typing.cast("typing.Any", ws).close = close
    req = type("Req", (), {"path": "/x", "path_template": ""})()

    with pytest.raises(TypeError):
        await router.on_websocket(req, ws)

    assert closed.get("closed") == 1000


@pytest.mark.asyncio
async def test_add_route_accepts_factory() -> None:
    """Verify that callable factories may be used as route targets."""
    created: dict[str, object] = {}

    class FactoryResource(WebSocketResource):
        def __init__(self, value: int) -> None:
            created["init"] = value

        async def on_connect(self, req: object, ws: object, **params: object) -> bool:
            created["params"] = params
            return False

    def factory(value: int) -> FactoryResource:
        return FactoryResource(value)

    router = WebSocketRouter()
    router.add_route("/f/{id}", factory, args=(7,), name="factory")
    router.mount("/")

    req = type("Req", (), {"path": "/f/5", "path_template": ""})()
    await router.on_websocket(req, DummyWS())

    assert created == {"init": 7, "params": {"id": "5"}}


@pytest.mark.asyncio
async def test_router_mount_on_app() -> None:
    """Verify routers work when mounted on a Falcon ``App``."""
    DummyResource.instances.clear()
    router = WebSocketRouter()
    router.add_route("/rooms/{room}", DummyResource, name="room")
    router.mount("/ws")

    app = falcon.asgi.App()
    app.add_route("/ws", router)

    req = type("Req", (), {"path": "/ws/rooms/42", "path_template": "/ws"})()
    await router.on_websocket(req, DummyWS())
    assert DummyResource.instances[-1].params == {"room": "42"}
