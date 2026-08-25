"""Unit tests covering the hook manager orchestration."""

from __future__ import annotations

import asyncio
import typing as typ

import pytest

from falcon_pachinko import (
    HookCollection,
    HookContext,
    HookManager,
    WebSocketResource,
    WebSocketRouter,
)
from falcon_pachinko.resource import _receive_hooks
from falcon_pachinko.unittests.helpers import DummyWS

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from falcon_pachinko.hooks import HookCallable


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
) -> cabc.Callable[[HookContext], None]:
    """Produce a global hook that records context assertions."""

    def global_hook(context: HookContext) -> None:
        assert isinstance(context.target, HookChild), (
            "global hook should target the connecting child resource"
        )
        match context.event:
            case "before_connect":
                if context.params is None:
                    context.params = {}
                context.params.setdefault("global", True)
            case "after_connect":
                assert context.result is True, "on_connect should have returned True"
            case "after_receive":
                assert context.error is None, "no error expected during dispatch"
        events.append(f"global.{context.event}")

    return global_hook


def create_parent_hook(
    events: list[str],
) -> cabc.Callable[[HookContext], None]:
    """Produce a parent-level hook for order verification."""

    def parent_hook(context: HookContext) -> None:
        assert context.resource in HookParent.instances, (
            "parent hook should run against a known HookParent instance"
        )
        match context.event:
            case "before_connect":
                if context.params is None:
                    context.params = {}
                context.params.setdefault("parent", True)
            case "after_receive":
                assert context.error is None, "no error expected during dispatch"
        events.append(f"parent.{context.event}")

    return parent_hook


def create_child_hook(
    events: list[str],
) -> cabc.Callable[[HookContext], None]:
    """Produce a child-level hook that inspects payload flow."""

    def child_hook(context: HookContext) -> None:
        assert isinstance(context.target, HookChild), (
            "child hook should target the connecting child resource"
        )
        match context.event:
            case "after_connect":
                assert context.result is True, "on_connect should have returned True"
            case "before_receive":
                assert context.raw == b'{"type":"noop"}', (
                    "the dispatched no-op payload should be visible before receive"
                )
            case "after_receive":
                assert context.error is None, "no error expected during dispatch"
        events.append(f"child.{context.event}")

    return child_hook


class BoomResource(WebSocketResource):
    """Resource whose handler always raises, for error-reporting hook tests."""

    instances: typ.ClassVar[list[BoomResource]] = []

    def __init__(self) -> None:
        BoomResource.instances.append(self)

    async def on_connect(self, req: object, ws: object, **params: object) -> bool:
        """Accept every connection."""
        return True

    async def on_boom(self, ws: object, payload: object) -> None:
        """Raise to exercise after-hook error reporting."""
        raise RuntimeError("boom")


def create_error_reporting_global_hook(
    events: list[tuple[str, str]],
) -> cabc.Callable[[HookContext], None]:
    """Produce a global hook recording events and checking the reported error."""

    def global_hook(context: HookContext) -> None:
        events.append(("global", context.event))
        if context.event == "after_receive":
            assert isinstance(context.error, RuntimeError), (
                "after_receive should report the handler's RuntimeError"
            )

    return global_hook


def create_error_reporting_resource_hook(
    events: list[tuple[str, str]],
) -> cabc.Callable[[HookContext], None]:
    """Produce a resource hook recording events and checking raw/error state."""

    def resource_hook(context: HookContext) -> None:
        events.append(("resource", context.event))
        if context.event == "before_receive":
            assert context.raw == b'{"type":"boom"}', (
                "before_receive should see the raw payload that triggers the error"
            )
        if context.event == "after_receive":
            assert isinstance(context.error, RuntimeError), (
                "after_receive should report the handler's RuntimeError"
            )

    return resource_hook


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
def reset_hook_state() -> cabc.Iterator[None]:
    """Ensure per-test isolation for hook registries and instances."""
    HookParent.hooks = HookCollection()
    HookChild.hooks = HookCollection()
    BoomResource.hooks = HookCollection()
    HookParent.instances = []
    HookChild.instances = []
    BoomResource.instances = []
    HookChild._events = []
    yield
    HookParent.hooks = HookCollection()
    HookChild.hooks = HookCollection()
    BoomResource.hooks = HookCollection()
    HookParent.instances = []
    HookChild.instances = []
    BoomResource.instances = []
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

    expected_order = [
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
    assert hook_test_environment.events == expected_order, (
        "hooks should fire in onion order across all three scopes"
    )


@pytest.mark.asyncio
async def test_hook_context_parameter_propagation(
    hook_test_environment: HookTestEnvironment,
) -> None:
    """Before-connect hooks may amend params passed to the resource."""
    child = await hook_test_environment.open_connection()

    assert child.params["global"] is True, (
        "global before_connect hook should have set the global flag"
    )
    assert child.params["parent"] is True, (
        "parent before_connect hook should have set the parent flag"
    )


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
    ], "receive hooks should surround handler dispatch in onion order"


def test_hookcollection_add_unsupported_event() -> None:
    """Registering an unknown event raises ``ValueError``."""
    collection = HookCollection()
    with pytest.raises(ValueError, match="Unsupported hook event"):
        collection.add("unsupported_event", dummy_hook)


def test_hookcollection_add_non_callable() -> None:
    """Registering a non-callable hook raises ``TypeError``."""
    collection = HookCollection()
    bad_hook = typ.cast("HookCallable", typ.cast("object", "not_a_callable"))
    with pytest.raises(TypeError, match="hook must be callable"):
        collection.add("before_connect", bad_hook)


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
    assert parent_before in Parent.hooks.iter("before_receive"), (
        "parent hook should be registered on the parent"
    )
    assert parent_before in Child.hooks.iter("before_receive"), (
        "child should inherit hooks registered on the parent after the fact"
    )

    Child.hooks.add("after_receive", child_after)
    assert child_after in Child.hooks.iter("after_receive"), (
        "child-only hook should be registered on the child"
    )
    assert child_after not in Parent.hooks.iter("after_receive"), (
        "child-only hook should not leak back to the parent"
    )


@pytest.mark.asyncio
async def test_receive_hooks_skip_cancelled_error() -> None:
    """Cancelled dispatches propagate without invoking after hooks."""

    class CancelResource(WebSocketResource):
        pass

    events: list[str] = []

    def after_hook(context: HookContext) -> None:
        events.append(context.event)

    CancelResource.hooks.add("after_receive", after_hook)
    resource = CancelResource()
    manager = HookManager(global_hooks=HookCollection(), resources=(resource,))
    resource.bind_hook_manager(manager)

    async def run() -> None:
        async with _receive_hooks(manager, resource, ws=DummyWS(), raw=b"noop"):
            raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await run()

    assert not events, "after hooks should not run when the dispatch is cancelled"


@pytest.mark.asyncio
async def test_after_receive_reports_errors() -> None:
    """After hooks receive the raised exception."""
    events: list[tuple[str, str]] = []
    global_hook = create_error_reporting_global_hook(events)
    resource_hook = create_error_reporting_resource_hook(events)

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

    expected_events = [
        ("global", "before_receive"),
        ("resource", "before_receive"),
        ("resource", "after_receive"),
        ("global", "after_receive"),
    ]
    assert events == expected_events, (
        "before/after receive hooks should observe both layers around the error"
    )
