"""Behavioural tests for the hook orchestration system."""

from __future__ import annotations

import asyncio
import typing as typ
from types import SimpleNamespace

import pytest
from pytest_bdd import given, scenario, then, when

from falcon_pachinko import (
    HookCollection,
    HookContext,
    WebSocketResource,
    WebSocketRouter,
)
from falcon_pachinko.unittests.helpers import DummyWS

EVENTS: list[str] = []
AFTER_RECEIVE_ERRORS: list[tuple[str, BaseException | None]] = []


class HookedChild(WebSocketResource):
    """Child resource used to verify hook ordering."""

    instances: typ.ClassVar[list[HookedChild]] = []

    def __init__(self) -> None:
        HookedChild.instances.append(self)

    async def on_connect(self, req: object, ws: object, **params: object) -> bool:
        """Accept the connection and retain the provided params."""
        self.params = params
        return True

    async def on_unhandled(self, ws: object, message: str | bytes) -> None:
        """Record unhandled messages to observe hook ordering."""
        EVENTS.append("handler.child")

    async def on_error(self, ws: object, payload: object | None) -> None:
        """Raise an error to exercise hook propagation."""
        raise ValueError


class HookedParent(WebSocketResource):
    """Parent resource that mounts :class:`HookedChild`."""

    def __init__(self) -> None:
        self.add_subroute("child", HookedChild)


async def global_hook(context: HookContext) -> None:
    """Record global hook invocations and mutate connect params."""
    EVENTS.append(f"global.{context.event}")
    if context.event == "before_connect":
        if context.params is None:
            context.params = {}
        context.params.setdefault("global", True)
    elif context.event == "after_connect":
        assert context.result is True
    elif context.event == "after_receive":
        AFTER_RECEIVE_ERRORS.append(("global", context.error))


async def parent_hook(context: HookContext) -> None:
    """Capture parent-level hooks for ordering assertions."""
    EVENTS.append(f"parent.{context.event}")
    if context.event == "before_connect":
        if context.params is None:
            context.params = {}
        context.params.setdefault("parent", True)
    elif context.event == "after_receive":
        AFTER_RECEIVE_ERRORS.append(("parent", context.error))


async def child_hook(context: HookContext) -> None:
    """Capture child-level hooks for ordering assertions."""
    EVENTS.append(f"child.{context.event}")
    if context.event == "before_receive":
        assert context.raw is not None
    elif context.event == "after_receive":
        AFTER_RECEIVE_ERRORS.append(("child", context.error))


@scenario("features/hooks.feature", "Global and resource hooks wrap lifecycle")
def test_hooks_feature() -> None:
    """Scenario placeholder for pytest-bdd."""


@pytest.fixture(autouse=True)
def reset_hooks() -> typ.Iterator[None]:
    """Reset hook registries and accumulated events between scenarios."""
    HookedParent.hooks = HookCollection.inherit(WebSocketResource.hooks)
    HookedChild.hooks = HookCollection.inherit(WebSocketResource.hooks)
    HookedChild.instances.clear()
    EVENTS.clear()
    AFTER_RECEIVE_ERRORS.clear()
    yield
    HookedParent.hooks = HookCollection.inherit(WebSocketResource.hooks)
    HookedChild.hooks = HookCollection.inherit(WebSocketResource.hooks)
    HookedChild.instances.clear()
    EVENTS.clear()
    AFTER_RECEIVE_ERRORS.clear()


@pytest.fixture
def context() -> dict[str, typ.Any]:
    """Scenario-scoped context object used for step communication."""
    return {}


def _assert_event_sequence(
    context: dict[str, typ.Any],
    start_idx: int,
    end_idx: int | None,
    expected_events: list[str],
) -> None:
    """Helper function to validate event sequence ordering."""  # noqa: D401
    events: list[str] = context["events"]
    actual_slice = events[start_idx:] if end_idx is None else events[start_idx:end_idx]
    assert actual_slice == expected_events


@given("a router with multi-tier hooks")
def given_router(context: dict[str, typ.Any]) -> None:
    """Prepare a router with global and resource hooks."""
    router = WebSocketRouter()
    router.global_hooks.add("before_connect", global_hook)
    router.global_hooks.add("after_connect", global_hook)
    router.global_hooks.add("before_receive", global_hook)
    router.global_hooks.add("after_receive", global_hook)

    HookedParent.hooks.add("before_connect", parent_hook)
    HookedParent.hooks.add("after_connect", parent_hook)
    HookedParent.hooks.add("before_receive", parent_hook)
    HookedParent.hooks.add("after_receive", parent_hook)

    HookedChild.hooks.add("before_connect", child_hook)
    HookedChild.hooks.add("after_connect", child_hook)
    HookedChild.hooks.add("before_receive", child_hook)
    HookedChild.hooks.add("after_receive", child_hook)

    router.add_route("/hooks", HookedParent)
    router.mount("/")
    context["router"] = router


@given("a router with only global hooks")
def given_router_global_only(context: dict[str, typ.Any]) -> None:
    """Prepare a router that only registers global hooks."""
    router = WebSocketRouter()
    router.global_hooks.add("before_connect", global_hook)
    router.global_hooks.add("after_connect", global_hook)
    router.global_hooks.add("before_receive", global_hook)
    router.global_hooks.add("after_receive", global_hook)

    router.add_route("/hooks", HookedParent)
    router.mount("/")
    context["router"] = router


@when("a client connects and sends a message")
def when_client_connects(context: dict[str, typ.Any]) -> None:
    """Simulate a connection followed by a dispatched message."""
    router: WebSocketRouter = context["router"]
    ws = DummyWS()
    req = SimpleNamespace(path="/hooks/child", path_template="")
    asyncio.run(router.on_websocket(req, ws))

    child = HookedChild.instances[-1]
    context["child_params"] = child.params

    asyncio.run(child.dispatch(ws, b'{"type":"noop"}'))
    context["events"] = list(EVENTS)
    context["after_errors"] = list(AFTER_RECEIVE_ERRORS)


@when("a client connects and sends a message that triggers an error")
def when_client_connects_with_error(context: dict[str, typ.Any]) -> None:
    """Simulate a connection followed by a dispatched message that raises."""
    router: WebSocketRouter = context["router"]
    ws = DummyWS()
    req = SimpleNamespace(path="/hooks/child", path_template="")
    asyncio.run(router.on_websocket(req, ws))

    child = HookedChild.instances[-1]
    context["child_params"] = child.params

    try:
        asyncio.run(child.dispatch(ws, b'{"type":"error"}'))
    except Exception as exc:  # noqa: BLE001 - surface the raised error
        context["error"] = exc

    context["events"] = list(EVENTS)
    context["after_errors"] = list(AFTER_RECEIVE_ERRORS)


@then("the hook log should show layered connect order")
def then_connect_order(context: dict[str, typ.Any]) -> None:
    """Validate connect hook execution ordering."""
    _assert_event_sequence(
        context,
        0,
        6,
        [
            "global.before_connect",
            "parent.before_connect",
            "child.before_connect",
            "child.after_connect",
            "parent.after_connect",
            "global.after_connect",
        ],
    )


@then("the hook log should show layered receive order")
def then_receive_order(context: dict[str, typ.Any]) -> None:
    """Validate receive hook execution ordering."""
    _assert_event_sequence(
        context,
        6,
        None,
        [
            "global.before_receive",
            "parent.before_receive",
            "child.before_receive",
            "handler.child",
            "child.after_receive",
            "parent.after_receive",
            "global.after_receive",
        ],
    )
    assert context["after_errors"] == [
        ("child", None),
        ("parent", None),
        ("global", None),
    ]


@then("only global hooks are recorded")
def then_only_global_hooks(context: dict[str, typ.Any]) -> None:
    """Ensure only router-level hooks executed for the scenario."""
    assert context["events"] == [
        "global.before_connect",
        "global.after_connect",
        "global.before_receive",
        "handler.child",
        "global.after_receive",
    ]
    params = context["child_params"]
    assert params["global"] is True
    assert "parent" not in params
    assert context["after_errors"] == [("global", None)]


@then("the child resource records hook-injected params")
def then_child_params(context: dict[str, typ.Any]) -> None:
    """Ensure context mutation from hooks reaches the child resource."""
    params = context["child_params"]
    assert params["global"] is True
    assert params["parent"] is True


@scenario(
    "features/hooks.feature",
    "Errors propagate through after hooks",
)
def test_hooks_error_feature() -> None:
    """Scenario placeholder for pytest-bdd error propagation."""


@then("the error is propagated to after_receive hook and the hook chain remains intact")
def then_error_propagates(context: dict[str, typ.Any]) -> None:
    """Verify that after hooks observed the raised error in order."""
    assert "error" in context
    assert isinstance(context["error"], ValueError)
    assert context["events"][-3:] == [
        "child.after_receive",
        "parent.after_receive",
        "global.after_receive",
    ]
    assert context["after_errors"] == [
        ("child", context["error"]),
        ("parent", context["error"]),
        ("global", context["error"]),
    ]
