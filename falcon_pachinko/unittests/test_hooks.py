"""Unit tests covering the hook manager orchestration."""

from __future__ import annotations

import typing as typ

import pytest

from falcon_pachinko import (
    HookCollection,
    HookContext,
    WebSocketResource,
    WebSocketRouter,
)
from falcon_pachinko.unittests.helpers import DummyWS


def dummy_hook(context: HookContext) -> None:
    """No-op hook used for validation tests."""
    _ = context


class HookChild(WebSocketResource):
    """Child resource used to validate hook orchestration."""

    instances: typ.ClassVar[list[HookChild]] = []
    _events: typ.ClassVar[list[str]] = []

    def __init__(self) -> None:
        HookChild.instances.append(self)
        self.params: dict[str, object] = {}

    async def on_connect(self, req: object, ws: object, **params: object) -> bool:
        """Capture connection parameters for later assertions."""
        self.params = params
        return True

    async def on_unhandled(self, ws: object, message: str | bytes) -> None:
        """Record handler invocation for ordering validation."""
        self._events.append("handler.child")


class HookParent(WebSocketResource):
    """Parent resource that mounts ``HookChild``."""

    instances: typ.ClassVar[list[HookParent]] = []

    def __init__(self) -> None:
        HookParent.instances.append(self)
        self.add_subroute("child", HookChild)


def create_global_hook(
    events: list[str],
) -> typ.Callable[[HookContext], typ.Awaitable[None]]:
    """Produce a global hook that records context assertions."""

    async def global_hook(context: HookContext) -> None:
        assert isinstance(context.target, HookChild)
        if context.event == "before_connect":
            if context.params is None:
                context.params = {}
            context.params.setdefault("global", True)
        elif context.event == "after_connect":
            assert context.result is True
        elif context.event == "after_receive":
            assert context.error is None
        events.append(f"global.{context.event}")

    return global_hook


def create_parent_hook(
    events: list[str],
) -> typ.Callable[[HookContext], typ.Awaitable[None]]:
    """Produce a parent-level hook for order verification."""

    async def parent_hook(context: HookContext) -> None:
        assert context.resource in HookParent.instances
        if context.event == "before_connect":
            if context.params is None:
                context.params = {}
            context.params.setdefault("parent", True)
        elif context.event == "after_receive":
            assert context.error is None
        events.append(f"parent.{context.event}")

    return parent_hook


def create_child_hook(
    events: list[str],
) -> typ.Callable[[HookContext], typ.Awaitable[None]]:
    """Produce a child-level hook that inspects payload flow."""

    async def child_hook(context: HookContext) -> None:
        assert isinstance(context.target, HookChild)
        if context.event == "after_connect":
            assert context.result is True
        elif context.event == "before_receive":
            assert context.raw == b'{"type":"noop"}'
        elif context.event == "after_receive":
            assert context.error is None
        events.append(f"child.{context.event}")

    return child_hook


class HookTestEnvironment:
    """Encapsulate router, hooks, and connection state for tests."""

    def __init__(self) -> None:
        self.events: list[str] = []
        HookChild._events = self.events
        HookChild.instances = []
        HookParent.instances = []
        self.router = WebSocketRouter()
        self._ws: DummyWS | None = None
        self._register_hooks()

    def _register_hooks(self) -> None:
        global_hook = create_global_hook(self.events)
        parent_hook = create_parent_hook(self.events)
        child_hook = create_child_hook(self.events)

        self.router.global_hooks.add("before_connect", global_hook)
        self.router.global_hooks.add("after_connect", global_hook)
        self.router.global_hooks.add("before_receive", global_hook)
        self.router.global_hooks.add("after_receive", global_hook)

        HookParent.hooks.add("before_connect", parent_hook)
        HookParent.hooks.add("after_connect", parent_hook)
        HookParent.hooks.add("before_receive", parent_hook)
        HookParent.hooks.add("after_receive", parent_hook)

        HookChild.hooks.add("before_connect", child_hook)
        HookChild.hooks.add("after_connect", child_hook)
        HookChild.hooks.add("before_receive", child_hook)
        HookChild.hooks.add("after_receive", child_hook)

        self.router.add_route("/hooks", HookParent)
        self.router.mount("/")

    async def open_connection(self) -> HookChild:
        """Create a connection and return the instantiated child resource."""
        self._ws = DummyWS()
        req = type("Req", (), {"path": "/hooks/child", "path_template": ""})()
        await self.router.on_websocket(req, self._ws)
        return HookChild.instances[-1]

    async def dispatch_noop(self, child: HookChild) -> None:
        """Send a no-op payload through the active connection."""
        assert self._ws is not None, (
            "call open_connection() before dispatching messages"
        )
        await child.dispatch(self._ws, b'{"type":"noop"}')


@pytest.fixture(autouse=True)
def reset_hook_state() -> typ.Iterator[None]:
    """Ensure per-test isolation for hook registries and instances."""
    HookParent.hooks = HookCollection()
    HookChild.hooks = HookCollection()
    HookParent.instances = []
    HookChild.instances = []
    HookChild._events = []
    yield
    HookParent.hooks = HookCollection()
    HookChild.hooks = HookCollection()
    HookParent.instances = []
    HookChild.instances = []
    HookChild._events = []


@pytest.fixture
def hook_test_environment() -> HookTestEnvironment:
    """Provide a configured hook scenario for tests."""
    return HookTestEnvironment()


@pytest.mark.asyncio
async def test_hooks_execute_in_layered_order(
    hook_test_environment: HookTestEnvironment,
) -> None:
    """Hooks fire in onion order across global, parent, and child scopes."""
    child = await hook_test_environment.open_connection()
    await hook_test_environment.dispatch_noop(child)

    assert hook_test_environment.events == [
        "global.before_connect",
        "parent.before_connect",
        "child.before_connect",
        "child.after_connect",
        "parent.after_connect",
        "global.after_connect",
        "global.before_receive",
        "parent.before_receive",
        "child.before_receive",
        "handler.child",
        "child.after_receive",
        "parent.after_receive",
        "global.after_receive",
    ]


@pytest.mark.asyncio
async def test_hook_context_parameter_propagation(
    hook_test_environment: HookTestEnvironment,
) -> None:
    """Before-connect hooks may amend params passed to the resource."""
    child = await hook_test_environment.open_connection()

    assert child.params["global"] is True
    assert child.params["parent"] is True


@pytest.mark.asyncio
async def test_message_processing_hooks_capture_handler_events(
    hook_test_environment: HookTestEnvironment,
) -> None:
    """Receive hooks surround dispatch and observe handler execution."""
    child = await hook_test_environment.open_connection()
    await hook_test_environment.dispatch_noop(child)

    assert hook_test_environment.events[6:] == [
        "global.before_receive",
        "parent.before_receive",
        "child.before_receive",
        "handler.child",
        "child.after_receive",
        "parent.after_receive",
        "global.after_receive",
    ]


def test_hookcollection_add_unsupported_event() -> None:
    """Registering an unknown event raises ``ValueError``."""
    collection = HookCollection()
    with pytest.raises(ValueError, match="Unsupported hook event"):
        collection.add("unsupported_event", dummy_hook)


def test_hookcollection_add_non_callable() -> None:
    """Registering a non-callable hook raises ``TypeError``."""
    collection = HookCollection()
    with pytest.raises(TypeError, match="hook must be callable"):
        collection.add("before_connect", "not_a_callable")


def test_hookcollection_inheritance_propagates_changes() -> None:
    """Child classes observe parent hook registrations added later."""

    class Parent(WebSocketResource):
        pass

    class Child(Parent):
        pass

    def parent_before(context: HookContext) -> None:
        return None

    def child_after(context: HookContext) -> None:
        return None

    Parent.hooks.add("before_receive", parent_before)
    assert parent_before in Parent.hooks.iter("before_receive")
    assert parent_before in Child.hooks.iter("before_receive")

    Child.hooks.add("after_receive", child_after)
    assert child_after in Child.hooks.iter("after_receive")
    assert child_after not in Parent.hooks.iter("after_receive")


@pytest.mark.asyncio
async def test_after_receive_reports_errors() -> None:
    """After hooks receive the raised exception."""
    events: list[tuple[str, str]] = []

    class BoomResource(WebSocketResource):
        instances: typ.ClassVar[list[BoomResource]] = []

        def __init__(self) -> None:
            BoomResource.instances.append(self)

        async def on_connect(self, req: object, ws: object, **params: object) -> bool:
            return True

        async def on_boom(self, ws: object, payload: object) -> None:
            raise RuntimeError("boom")

    async def global_hook(context: HookContext) -> None:
        events.append(("global", context.event))
        if context.event == "after_receive":
            assert isinstance(context.error, RuntimeError)

    async def resource_hook(context: HookContext) -> None:
        events.append(("resource", context.event))
        if context.event == "before_receive":
            assert context.raw == b'{"type":"boom"}'
        if context.event == "after_receive":
            assert isinstance(context.error, RuntimeError)

    router = WebSocketRouter()
    router.global_hooks.add("before_receive", global_hook)
    router.global_hooks.add("after_receive", global_hook)

    BoomResource.hooks.add("before_receive", resource_hook)
    BoomResource.hooks.add("after_receive", resource_hook)

    router.add_route("/boom", BoomResource)
    router.mount("/")

    ws = DummyWS()
    req = type("Req", (), {"path": "/boom", "path_template": ""})()
    await router.on_websocket(req, ws)

    resource = BoomResource.instances[-1]
    with pytest.raises(RuntimeError):
        await resource.dispatch(ws, b'{"type":"boom"}')

    assert events == [
        ("global", "before_receive"),
        ("resource", "before_receive"),
        ("resource", "after_receive"),
        ("global", "after_receive"),
    ]
