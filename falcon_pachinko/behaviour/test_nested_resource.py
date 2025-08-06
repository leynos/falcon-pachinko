"""Behavioural tests for nested subroutes."""

from __future__ import annotations

import asyncio
import typing
from types import SimpleNamespace

import falcon
import pytest
from pytest_bdd import given, scenario, then, when

from falcon_pachinko import WebSocketResource, WebSocketRouter
from falcon_pachinko.unittests.helpers import DummyWS


@scenario("features/nested_resource.feature", "Connect to nested child resource")
def test_connect_to_nested_child_resource() -> None:
    """Scenario: Connect to nested child resource."""


@scenario("features/nested_resource.feature", "Unmatched nested path returns 404")
def test_unmatched_nested_path_returns_404() -> None:
    """Scenario: Unmatched nested path returns 404."""


@scenario("features/nested_resource.feature", "Connect to deeply nested grandchild")
def test_connect_to_grandchild() -> None:
    """Scenario: Connect to deeply nested grandchild."""


@scenario(
    "features/nested_resource.feature",
    "Parameter shadowing overrides parent value",
)
def test_parameter_shadowing() -> None:
    """Scenario: Parameter shadowing overrides parent value."""


@scenario(
    "features/nested_resource.feature",
    "Parent passes context to child resource",
)
def test_parent_context_passing() -> None:
    """Scenario: Parent passes context to child resource."""


@pytest.fixture
def context() -> dict[str, typing.Any]:
    """Scenario-scoped context object."""
    return {}


class ChildResource(WebSocketResource):
    """Record connection parameters."""

    instances: typing.ClassVar[list[ChildResource]] = []

    def __init__(self) -> None:
        """Track instances for assertions."""
        ChildResource.instances.append(self)
        self.add_subroute("{cid}/grandchild", GrandchildResource)

    async def on_connect(self, req: object, ws: object, **params: object) -> bool:
        """Store params and refuse the connection."""
        self.params = params
        return False


class GrandchildResource(WebSocketResource):
    """Record grandchild connection parameters."""

    instances: typing.ClassVar[list[GrandchildResource]] = []

    def __init__(self) -> None:
        GrandchildResource.instances.append(self)

    async def on_connect(self, req: object, ws: object, **params: object) -> bool:
        """Record params and refuse connection."""
        self.params = params
        return False


class ParentResource(WebSocketResource):
    """Resource that mounts ``ChildResource``."""

    def __init__(self) -> None:
        """Register the child subroute."""
        self.add_subroute("child", ChildResource)

    async def on_connect(self, req: object, ws: object, **params: object) -> bool:
        """Store params for later inspection."""
        self.params = params
        return False


class ShadowChildResource(WebSocketResource):
    """Child resource used to test parameter shadowing."""

    instances: typing.ClassVar[list[ShadowChildResource]] = []

    def __init__(self) -> None:
        """Track instance creation for assertions."""
        ShadowChildResource.instances.append(self)

    async def on_connect(self, req: object, ws: object, **params: object) -> bool:
        """Record params and refuse the connection."""
        self.params = params
        return False


class ShadowParentResource(WebSocketResource):
    """Parent resource that declares same-named parameter on subroute."""

    def __init__(self) -> None:
        """Register subroute with matching parameter name."""
        self.add_subroute("{pid}", ShadowChildResource)

    async def on_connect(self, req: object, ws: object, **params: object) -> bool:
        """Record params for later inspection."""
        self.params = params
        return False


class CtxChildResource(WebSocketResource):
    """Child resource that records injected project and shared state."""

    instances: typing.ClassVar[list[CtxChildResource]] = []

    def __init__(self, project: str) -> None:
        """Track project and instance for assertions."""
        self.project = project
        CtxChildResource.instances.append(self)

    async def on_connect(self, req: object, ws: object, **params: object) -> bool:
        """Record participation in connection."""
        self.state["child"] = True
        return False


class CtxParentResource(WebSocketResource):
    """Parent resource that injects context into its child."""

    instances: typing.ClassVar[list[CtxParentResource]] = []

    def __init__(self) -> None:
        """Register child, track instance, and seed state."""
        self.project = "acme"
        self.state["parent"] = True
        self.add_subroute("child", CtxChildResource)
        CtxParentResource.instances.append(self)

    def get_child_context(self) -> dict[str, object]:
        """Provide constructor kwargs for child resource."""
        return {"project": self.project}

    async def on_connect(self, req: object, ws: object, **params: object) -> bool:
        """No-op connect handler for tests."""
        return False


@given("a router with a nested child resource")
def setup_router(context: dict[str, typing.Any]) -> None:
    """Prepare router and clear previous instances."""
    ChildResource.instances.clear()
    GrandchildResource.instances.clear()
    router = WebSocketRouter()
    router.add_route("/parents/{pid}", ParentResource)
    router.mount("/")
    context["router"] = router


@given("a router with parameter shadowing resources")
def setup_shadow_router(context: dict[str, typing.Any]) -> None:
    """Prepare router for parameter shadowing scenario."""
    ShadowChildResource.instances.clear()
    router = WebSocketRouter()
    router.add_route("/shadow/{pid}", ShadowParentResource)
    router.mount("/")
    context["router"] = router


@given("a router with context-passing resources")
def setup_context_router(context: dict[str, typing.Any]) -> None:
    """Prepare router for context-passing scenario."""
    CtxChildResource.instances.clear()
    CtxParentResource.instances.clear()
    router = WebSocketRouter()
    router.add_route("/ctx", CtxParentResource)
    router.mount("/")
    context["router"] = router


def _simulate_connection(
    context: dict[str, typing.Any],
    path: str,
    *,
    capture_exceptions: bool = False,
) -> None:
    """Simulate a WebSocket connection."""
    router: WebSocketRouter = context["router"]
    ws = DummyWS()
    req = typing.cast("falcon.Request", SimpleNamespace(path=path, path_template=""))
    if capture_exceptions:
        try:
            asyncio.run(router.on_websocket(req, ws))
        except falcon.HTTPNotFound as exc:
            context["exception"] = exc
    else:
        asyncio.run(router.on_websocket(req, ws))


@when('a client connects to "/parents/42/child"')
def connect_child(context: dict[str, typing.Any]) -> None:
    """Simulate connecting to the child path."""
    _simulate_connection(context, "/parents/42/child")


@when('a client connects to "/parents/42/missing"')
def connect_missing(context: dict[str, typing.Any]) -> None:
    """Attempt connection to an invalid path."""
    _simulate_connection(context, "/parents/42/missing", capture_exceptions=True)


@when('a client connects to "/parents/42/child/99/grandchild"')
def connect_grandchild(context: dict[str, typing.Any]) -> None:
    """Simulate connecting to a grandchild path."""
    _simulate_connection(context, "/parents/42/child/99/grandchild")


@when('a client connects to "/shadow/1/2"')
def connect_shadow_child(context: dict[str, typing.Any]) -> None:
    """Connect to the shadow child path."""
    _simulate_connection(context, "/shadow/1/2")


@when('a client connects to "/ctx/child"')
def connect_ctx_child(context: dict[str, typing.Any]) -> None:
    """Connect to the context child path."""
    _simulate_connection(context, "/ctx/child")


@then('the child resource should receive params {"pid": "42"}')
def assert_child_params() -> None:
    """Verify child resource captured parent parameter."""
    assert ChildResource.instances[-1].params == {"pid": "42"}


@then("HTTPNotFound should be raised")
def assert_not_found(context: dict[str, typing.Any]) -> None:
    """Ensure ``HTTPNotFound`` was raised."""
    assert isinstance(context.get("exception"), falcon.HTTPNotFound)


@then('the grandchild resource should capture params {"pid": "42", "cid": "99"}')
def assert_grandchild_params() -> None:
    """Verify grandchild resource captured all params."""
    assert GrandchildResource.instances[-1].params == {"pid": "42", "cid": "99"}


@then('the shadow child resource should capture params {"pid": "2"}')
def assert_shadow_child_params() -> None:
    """Verify that child parameter overrides the parent's value."""
    assert ShadowChildResource.instances[-1].params == {"pid": "2"}


@then('the context child resource should receive project "acme"')
def assert_ctx_child_project() -> None:
    """Ensure child received injected project."""
    assert CtxChildResource.instances[-1].project == "acme"


@then("the shared state should contain flags from both resources")
def assert_ctx_shared_state() -> None:
    """Verify that parent and child share connection state."""
    parent = CtxParentResource.instances[-1]
    child = CtxChildResource.instances[-1]
    assert child.state is parent.state
    assert child.state == {"parent": True, "child": True}
