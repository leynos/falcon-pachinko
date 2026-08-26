"""Behavioural tests for the hook orchestration system."""

from __future__ import annotations

import asyncio
import typing as typ

import pytest
from pytest_bdd import given, scenario, then, when

if typ.TYPE_CHECKING:
    import collections.abc as cabc

from falcon_pachinko import (
    HookCollection,
    HookContext,
    WebSocketResource,
    WebSocketRouter,
)
from falcon_pachinko.unittests.helpers import DummyWS, make_req

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


def global_hook(context: HookContext) -> None:
    """Record global hook invocations and mutate connect params."""
    EVENTS.append(f"global.{context.event}")
    match context.event:
        case "before_connect":
            if context.params is None:
                context.params = {}
            context.params.setdefault("global", True)
        case "after_connect":
            assert context.result is True, "on_connect should have returned True"
        case "after_receive":
            AFTER_RECEIVE_ERRORS.append(("global", context.error))


def parent_hook(context: HookContext) -> None:
    """Capture parent-level hooks for ordering assertions."""
    EVENTS.append(f"parent.{context.event}")
    match context.event:
        case "before_connect":
            if context.params is None:
                context.params = {}
            context.params.setdefault("parent", True)
        case "after_receive":
            AFTER_RECEIVE_ERRORS.append(("parent", context.error))


def child_hook(context: HookContext) -> None:
    """Capture child-level hooks for ordering assertions."""
    EVENTS.append(f"child.{context.event}")
    match context.event:
        case "before_receive":
            assert context.raw is not None, (
                "raw payload should be visible before receive"
            )
        case "after_receive":
            AFTER_RECEIVE_ERRORS.append(("child", context.error))


@scenario("features/hooks.feature", "Global and resource hooks wrap lifecycle")
def test_hooks_feature() -> None:
    """Scenario placeholder for pytest-bdd."""


@pytest.fixture(autouse=True)
def reset_hooks() -> cabc.Iterator[None]:
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
    """Validate the recorded event sequence against ``expected_events``."""
    events: list[str] = context["events"]
    actual_slice = events[start_idx:] if end_idx is None else events[start_idx:end_idx]
    assert actual_slice == expected_events, (
        "hook events should appear in the expected order for this slice"
    )


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


def _connect_client(context: dict[str, typ.Any]) -> tuple[HookedChild, DummyWS]:
    """Run the connect lifecycle and record the params hooks injected."""
    router: WebSocketRouter = context["router"]
    ws = DummyWS()
    req = make_req("/hooks/child")
    asyncio.run(router.on_websocket(req, ws))

    child = HookedChild.instances[-1]
    context["child_params"] = child.params
    return child, ws


def _record_hook_observations(context: dict[str, typ.Any]) -> None:
    """Snapshot the hook event log and the errors after_receive observed."""
    context["events"] = list(EVENTS)
    context["after_errors"] = list(AFTER_RECEIVE_ERRORS)


@when("a client connects and sends a message")
def when_client_connects(context: dict[str, typ.Any]) -> None:
    """Simulate a connection followed by a dispatched message."""
    child, ws = _connect_client(context)

    asyncio.run(child.dispatch(ws, b'{"type":"noop"}'))
    _record_hook_observations(context)


@when("a client connects and sends a message that triggers an error")
def when_client_connects_with_error(context: dict[str, typ.Any]) -> None:
    """Simulate a connection followed by a dispatched message that raises."""
    child, ws = _connect_client(context)

    try:
        asyncio.run(child.dispatch(ws, b'{"type":"error"}'))
    except ValueError as exc:
        context["error"] = exc

    _record_hook_observations(context)


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
    ], "each layer's after_receive hook should observe no error"


@then("only global hooks are recorded")
def then_only_global_hooks(context: dict[str, typ.Any]) -> None:
    """Ensure only router-level hooks executed for the scenario."""
    assert context["events"] == [
        "global.before_connect",
        "global.after_connect",
        "global.before_receive",
        "handler.child",
        "global.after_receive",
    ], "only global hooks should have run when no resource hooks are registered"
    params = context["child_params"]
    assert params["global"] is True, "global before_connect hook should set its flag"
    assert "parent" not in params, "no parent hook was registered, so no parent flag"
    assert context["after_errors"] == [("global", None)], (
        "only the global after_receive hook should have recorded no error"
    )


@then("the child resource records hook-injected params")
def then_child_params(context: dict[str, typ.Any]) -> None:
    """Ensure context mutation from hooks reaches the child resource."""
    params = context["child_params"]
    assert params["global"] is True, "global before_connect hook should set its flag"
    assert params["parent"] is True, "parent before_connect hook should set its flag"


@scenario(
    "features/hooks.feature",
    "Errors propagate through after hooks",
)
def test_hooks_error_feature() -> None:
    """Scenario placeholder for pytest-bdd error propagation."""


@then("the error is propagated to after_receive hook and the hook chain remains intact")
def then_error_propagates(context: dict[str, typ.Any]) -> None:
    """Verify that after hooks observed the raised error in order."""
    assert "error" in context, "the raised error should have been captured"
    assert isinstance(context["error"], ValueError), (
        "the captured error should be the ValueError raised by on_error"
    )
    assert context["events"][-3:] == [
        "child.after_receive",
        "parent.after_receive",
        "global.after_receive",
    ], "after_receive hooks should still run in order despite the error"
    assert context["after_errors"] == [
        ("child", context["error"]),
        ("parent", context["error"]),
        ("global", context["error"]),
    ], "every layer's after_receive hook should observe the same error"
