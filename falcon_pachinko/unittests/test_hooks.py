"""Unit tests covering the hook manager orchestration."""

from __future__ import annotations

import typing as typ

import pytest

from falcon_pachinko import HookContext, WebSocketResource, WebSocketRouter
from falcon_pachinko.unittests.helpers import DummyWS


@pytest.mark.asyncio
async def test_hook_order_and_context() -> None:
    """Global and resource hooks execute in layered order."""
    events: list[str] = []

    class HookChild(WebSocketResource):
        """Child resource that records hook context."""

        instances: typ.ClassVar[list[HookChild]] = []

        def __init__(self) -> None:
            HookChild.instances.append(self)

        async def on_connect(self, req: object, ws: object, **params: object) -> bool:
            self.params = params
            return True

        async def on_unhandled(self, ws: object, message: str | bytes) -> None:
            events.append("handler.child")

    class HookParent(WebSocketResource):
        """Parent resource that mounts ``HookChild``."""

        instances: typ.ClassVar[list[HookParent]] = []

        def __init__(self) -> None:
            HookParent.instances.append(self)
            self.add_subroute("child", HookChild)

    async def global_hook(context: HookContext) -> None:
        assert isinstance(context.target, HookChild)
        if context.event == "before_connect":
            if context.params is None:
                context.params = {}
            context.params.setdefault("global", True)
        if context.event == "after_connect":
            assert context.result is True
        if context.event == "after_receive":
            assert context.error is None
        events.append(f"global.{context.event}")

    async def parent_hook(context: HookContext) -> None:
        assert context.resource in HookParent.instances
        if context.event == "before_connect":
            if context.params is None:
                context.params = {}
            context.params.setdefault("parent", True)
        if context.event == "after_receive":
            assert context.error is None
        events.append(f"parent.{context.event}")

    async def child_hook(context: HookContext) -> None:
        assert isinstance(context.target, HookChild)
        if context.event == "after_connect":
            assert context.result is True
        if context.event == "before_receive":
            assert context.raw == b'{"type":"noop"}'
        if context.event == "after_receive":
            assert context.error is None
        events.append(f"child.{context.event}")

    router = WebSocketRouter()
    router.global_hooks.add("before_connect", global_hook)
    router.global_hooks.add("after_connect", global_hook)
    router.global_hooks.add("before_receive", global_hook)
    router.global_hooks.add("after_receive", global_hook)

    HookParent.hooks.add("before_connect", parent_hook)
    HookParent.hooks.add("after_connect", parent_hook)
    HookParent.hooks.add("before_receive", parent_hook)
    HookParent.hooks.add("after_receive", parent_hook)

    HookChild.hooks.add("before_connect", child_hook)
    HookChild.hooks.add("after_connect", child_hook)
    HookChild.hooks.add("before_receive", child_hook)
    HookChild.hooks.add("after_receive", child_hook)

    router.add_route("/hooks", HookParent)
    router.mount("/")

    ws = DummyWS()
    req = type("Req", (), {"path": "/hooks/child", "path_template": ""})()
    await router.on_websocket(req, ws)

    child = HookChild.instances[-1]
    assert child.params["global"] is True
    assert child.params["parent"] is True

    await child.dispatch(ws, b'{"type":"noop"}')

    assert events == [
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
