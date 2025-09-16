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
    if context.event == "after_connect":
        assert context.result is True
    if context.event == "after_receive":
        assert context.error is None


async def parent_hook(context: HookContext) -> None:
    """Capture parent-level hooks for ordering assertions."""
    EVENTS.append(f"parent.{context.event}")
    if context.event == "before_connect":
        if context.params is None:
            context.params = {}
        context.params.setdefault("parent", True)
    if context.event == "after_receive":
        assert context.error is None


async def child_hook(context: HookContext) -> None:
    """Capture child-level hooks for ordering assertions."""
    EVENTS.append(f"child.{context.event}")
    if context.event == "before_receive":
        assert context.raw == b'{"type":"noop"}'
    if context.event == "after_receive":
        assert context.error is None


@scenario("features/hooks.feature", "Global and resource hooks wrap lifecycle")
def test_hooks_feature() -> None:
    """Scenario placeholder for pytest-bdd."""


def _reset_hooks() -> None:
    HookedParent.hooks = HookCollection()
    HookedChild.hooks = HookCollection()


@pytest.fixture
def context() -> dict[str, typ.Any]:
    """Scenario-scoped context object used for step communication."""

    return {}


@given("a router with multi-tier hooks")
def given_router(context: dict[str, typ.Any]) -> None:
    """Prepare a router with global and resource hooks."""
    EVENTS.clear()
    HookedChild.instances.clear()
    _reset_hooks()

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


@then("the hook log should show layered connect order")
def then_connect_order(context: dict[str, typ.Any]) -> None:
    """Validate connect hook execution ordering."""
    events: list[str] = context["events"]
    assert events[:6] == [
        "global.before_connect",
        "parent.before_connect",
        "child.before_connect",
        "child.after_connect",
        "parent.after_connect",
        "global.after_connect",
    ]


@then("the hook log should show layered receive order")
def then_receive_order(context: dict[str, typ.Any]) -> None:
    """Validate receive hook execution ordering."""
    events: list[str] = context["events"]
    assert events[6:] == [
        "global.before_receive",
        "parent.before_receive",
        "child.before_receive",
        "handler.child",
        "child.after_receive",
        "parent.after_receive",
        "global.after_receive",
    ]


@then("the child resource records hook-injected params")
def then_child_params(context: dict[str, typ.Any]) -> None:
    """Ensure context mutation from hooks reaches the child resource."""
    params = context["child_params"]
    assert params["global"] is True
    assert params["parent"] is True
