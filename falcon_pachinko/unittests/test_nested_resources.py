"""Tests for nested resource composition."""

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

if typ.TYPE_CHECKING:
    import collections.abc as cabc


class Child(WebSocketResource):
    """Capture parameters passed to ``on_connect``."""

    instances: typ.ClassVar[list[Child]] = []

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


def _named_hook(events: list[str], scope: str) -> cabc.Callable[[HookContext], None]:
    """Return a hook that records its scope and lifecycle event."""

    def hook(context: HookContext) -> None:
        """Record this scoped hook's lifecycle event."""
        events.append(f"{scope}.{context.event}")

    return hook


def _attach_resource_hooks(
    events: list[str], resources: tuple[tuple[type[WebSocketResource], str], ...]
) -> None:
    """Attach before and after hooks to each resource class."""
    for resource, scope in resources:
        resource.hooks = HookCollection()
        resource.hooks.add("before_connect", _named_hook(events, scope))
        resource.hooks.add("after_connect", _named_hook(events, scope))


def _create_literal_sibling_router(
    route_order: str,
    events: list[str],
    created: list[str],
    selected: list[str],
) -> WebSocketRouter:
    """Build competing nested literal routes and their hook chains."""

    class DottedChild(WebSocketResource):
        def __init__(self) -> None:
            """Record construction of the dotted sibling."""
            created.append("dotted")

        async def on_connect(self, req: object, ws: object, **params: object) -> bool:
            """Record selection of the dotted sibling."""
            selected.append("dotted")
            return False

    class SlashedChild(WebSocketResource):
        def __init__(self) -> None:
            """Record construction of the slash-delimited sibling."""
            created.append("slashed")

        async def on_connect(self, req: object, ws: object, **params: object) -> bool:
            """Record selection of the slash-delimited sibling."""
            selected.append("slashed")
            return False

    class ParentWithLiteralSiblings(WebSocketResource):
        def __init__(self) -> None:
            """Register both literal sibling routes in the requested order."""
            routes = [
                ("child.v1", DottedChild),
                ("child/v1", SlashedChild),
            ]
            if route_order == "slashed-first":
                routes.reverse()
            for path, resource in routes:
                self.add_subroute(path, resource)

    resources = (
        (ParentWithLiteralSiblings, "parent"),
        (DottedChild, "dotted"),
        (SlashedChild, "slashed"),
    )
    _attach_resource_hooks(events, resources)

    router = WebSocketRouter()
    router.global_hooks.add("before_connect", _named_hook(events, "global"))
    router.global_hooks.add("after_connect", _named_hook(events, "global"))
    router.add_route("/parent", ParentWithLiteralSiblings)
    router.mount("/")
    return router


@pytest.mark.asyncio
@pytest.mark.parametrize("route_order", ["dotted-first", "slashed-first"])
async def test_literal_nested_siblings_select_resources_and_hooks(
    route_order: str,
) -> None:
    """Nested literal siblings dispatch by exact text in either order."""
    events: list[str] = []
    created: list[str] = []
    selected: list[str] = []
    router = _create_literal_sibling_router(route_order, events, created, selected)

    expected_hook_orders = {
        "/parent/child.v1": (
            "dotted",
            [
                "global.before_connect",
                "parent.before_connect",
                "dotted.before_connect",
                "dotted.after_connect",
                "parent.after_connect",
                "global.after_connect",
            ],
        ),
        "/parent/child/v1": (
            "slashed",
            [
                "global.before_connect",
                "parent.before_connect",
                "slashed.before_connect",
                "slashed.after_connect",
                "parent.after_connect",
                "global.after_connect",
            ],
        ),
    }
    for path, (resource_name, expected_events) in expected_hook_orders.items():
        events.clear()
        created.clear()
        selected.clear()
        await router.on_websocket(make_req(path), DummyWS())
        assert created == [resource_name], "only the matching child should be created"
        assert selected == [resource_name], "only the matching child should connect"
        assert events == expected_events, (
            "before hooks should run global-parent-child and after hooks in reverse"
        )

    events.clear()
    created.clear()
    selected.clear()
    with pytest.raises(falcon.HTTPNotFound):
        await router.on_websocket(make_req("/parent/childxv1"), DummyWS())
    assert not created, "a near-miss should not create a child"
    assert not selected, "a near-miss should not select a child"
    assert not events, "a near-miss should not run lifecycle hooks"


@pytest.mark.asyncio
async def test_nested_parameter_merging_with_literal_sibling() -> None:
    """A parameter sibling retains parent and child connect parameters."""

    class LiteralChild(WebSocketResource):
        async def on_connect(self, req: object, ws: object, **params: object) -> bool:
            """Refuse the literal sibling connection used by this test."""
            return False

    class ParameterChild(WebSocketResource):
        instances: typ.ClassVar[list[ParameterChild]] = []
        params: dict[str, object]

        def __init__(self) -> None:
            """Retain the parameter child for merged-parameter assertions."""
            ParameterChild.instances.append(self)

        async def on_connect(self, req: object, ws: object, **params: object) -> bool:
            """Capture parameters merged from parent and child routes."""
            self.params = params
            return False

    class ParentWithMixedSiblings(WebSocketResource):
        def __init__(self) -> None:
            """Register literal and parameter siblings for the merge test."""
            self.add_subroute("child.v1", LiteralChild)
            self.add_subroute("thing/{cid}", ParameterChild)

    ParameterChild.instances.clear()
    router = WebSocketRouter()
    router.add_route("/parent/{pid}", ParentWithMixedSiblings)
    router.mount("/")
    await router.on_websocket(make_req("/parent/42/thing/9"), DummyWS())

    assert ParameterChild.instances[-1].params == {"pid": "42", "cid": "9"}, (
        "parent and nested parameters should still merge into on_connect"
    )


@pytest.mark.asyncio
async def test_nested_subroute_params() -> None:
    """Parameters from each route level are merged."""
    Child.instances.clear()
    router = WebSocketRouter()
    router.add_route("/parent/{pid}", Parent)
    router.mount("/")
    req = make_req("/parent/1/child/2")
    await router.on_websocket(req, DummyWS())

    assert Child.instances[-1].params == {
        "pid": "1",
        "cid": "2",
    }, "params from every route level should be merged into the child"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("path", "description"),
    [
        ("/parent/1/oops", "Unmatched nested path should raise HTTPNotFound"),
        ("/parent/1child/2", "Missing slash between segments should not match"),
    ],
    ids=["unmatched_path", "malformed_path"],
)
async def test_nested_subroute_not_found(path: str, description: str) -> None:
    """Test cases where nested routes should raise HTTPNotFound."""
    router = WebSocketRouter()
    router.add_route("/parent/{pid}", Parent)
    router.mount("/")
    req = make_req(path)
    with pytest.raises(falcon.HTTPNotFound):
        await router.on_websocket(req, DummyWS())


def test_add_subroute_invalid_resource() -> None:
    """add_subroute must reject non-callables."""
    r = WebSocketResource()
    # The cast smuggles a deliberately non-callable value past the signature
    # to exercise the runtime type check.
    with pytest.raises(TypeError):
        r.add_subroute("child", typ.cast("typ.Any", object()))


class ContextChild(WebSocketResource):
    """Resource that receives context from its parent."""

    instances: typ.ClassVar[list[ContextChild]] = []

    def __init__(self, project: str) -> None:
        """Record project and track instance."""
        self.project = project
        ContextChild.instances.append(self)

    async def on_connect(self, req: object, ws: object, **params: object) -> bool:
        """Mark that the child handled the connection."""
        self.state["child"] = True
        return False


class ContextParent(WebSocketResource):
    """Parent that injects context and shares state."""

    instances: typ.ClassVar[list[ContextParent]] = []

    def __init__(self) -> None:
        """Register child subroute, track instance, and seed state."""
        self.project = "acme"
        self.state["parent"] = True
        self.add_subroute("child", ContextChild)
        ContextParent.instances.append(self)

    def get_child_context(self) -> dict[str, object]:
        """Provide constructor kwargs for the child."""
        return {"project": self.project}

    async def on_connect(self, req: object, ws: object, **params: object) -> bool:
        """No-op connect handler for tests."""
        return False


async def _setup_and_run_nested_test(
    child_class: type[typ.Any],
    parent_class: type[typ.Any],
    route_path: str,
    request_path: str,
) -> tuple[typ.Any, typ.Any]:
    """Execute nested resource flow and return created instances."""
    child_class.instances.clear()
    parent_class.instances.clear()
    router = WebSocketRouter()
    router.add_route(route_path, parent_class)
    router.mount("/")
    req = make_req(request_path)
    await router.on_websocket(req, DummyWS())
    parent = parent_class.instances[-1]
    child = child_class.instances[-1]
    return parent, child


@pytest.mark.asyncio
async def test_context_passed_and_state_shared() -> None:
    """Parent-supplied context and state propagate to the child."""
    parent, child = await _setup_and_run_nested_test(
        ContextChild, ContextParent, "/ctx", "/ctx/child"
    )
    assert child.project == "acme", "parent-supplied context should set child.project"
    assert child.state is parent.state, "child should share the parent's state mapping"
    assert child.state == {
        "parent": True,
        "child": True,
    }, "both parent and child updates should be visible in the shared state"


class InjectedChild(WebSocketResource):
    """Resource that mutates its own state."""

    instances: typ.ClassVar[list[InjectedChild]] = []

    def __init__(self) -> None:
        """Track instances for inspection."""
        InjectedChild.instances.append(self)

    async def on_connect(self, req: object, ws: object, **params: object) -> bool:
        """Mark that the child handled the connection."""
        self.state["child"] = True
        return False


class InjectingParent(WebSocketResource):
    """Parent that injects custom state into the child."""

    instances: typ.ClassVar[list[InjectingParent]] = []

    def __init__(self) -> None:
        """Register child subroute and seed parent state."""
        self.state["parent"] = True
        self.add_subroute("child", InjectedChild)
        InjectingParent.instances.append(self)

    def get_child_context(self) -> dict[str, object]:
        """Provide a fresh state mapping for the child."""
        return {"state": {"injected": True}}

    async def on_connect(self, req: object, ws: object, **params: object) -> bool:
        """No-op connect handler for tests."""
        return False


@pytest.mark.asyncio
async def test_state_injected_via_context() -> None:
    """Explicit state injection should override the parent's state."""
    parent, child = await _setup_and_run_nested_test(
        InjectedChild, InjectingParent, "/inj", "/inj/child"
    )
    assert child.state is not parent.state, (
        "injected state should replace the shared state, not alias it"
    )
    assert child.state == {
        "injected": True,
        "child": True,
    }, "child should see both the injected and its own state updates"
    assert parent.state == {
        "parent": True,
    }, "parent's own state should be unaffected by the child's injected state"
