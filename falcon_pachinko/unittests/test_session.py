"""Tests for router-owned WebSocket receive sessions."""

from __future__ import annotations

import asyncio
import typing as typ

import msgspec as ms
import msgspec.json as msjson
import pytest

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    import falcon

from falcon_pachinko import (
    HookCollection,
    HookContext,
    ServiceContainer,
    WebSocketLike,
    WebSocketResource,
    WebSocketRouter,
    handles_message,
)
from falcon_pachinko.unittests.helpers import RecordingWS, make_req


class Ping(ms.Struct, tag="ping"):
    """A message that is acknowledged by the session resource."""

    payload: str


class Fail(ms.Struct, tag="fail"):
    """A message that makes the resource raise its recorded failure."""

    payload: str


MessageSchema = Ping | Fail


class SessionResource(WebSocketResource):
    """Record dispatch, fallback, reply, and disconnect activity."""

    schema = MessageSchema
    failures: typ.ClassVar[list[Exception]] = []

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.unhandled: list[str | bytes] = []
        self.disconnects: list[int] = []

    async def _respond_to_ping(self, ws: WebSocketLike, payload: Ping) -> None:
        self.calls.append(payload.payload)
        await ws.send_media({"type": "reply", "payload": payload.payload})

    @handles_message("ping")
    async def handle_ping(self, ws: WebSocketLike, payload: Ping) -> None:
        """Record a ping and acknowledge it on the same socket."""
        await self._respond_to_ping(ws, payload)

    @handles_message("fail")
    async def handle_fail(self, ws: WebSocketLike, payload: Fail) -> None:
        """Raise a stable exception instance for close-policy assertions."""
        del ws, payload
        failure = RuntimeError("handler failed")
        self.failures.append(failure)
        raise failure

    async def on_unhandled(self, ws: WebSocketLike, message: str | bytes) -> None:
        """Record unknown or malformed data and keep the connection usable."""
        self.unhandled.append(message)
        await ws.send_media({"type": "error", "payload": "unsupported message"})

    async def on_disconnect(self, ws: WebSocketLike, close_code: int) -> None:
        """Record the code chosen by the peer or session policy."""
        del ws
        self.disconnects.append(close_code)


def _router_for(
    resource: WebSocketResource,
    *,
    resource_factory: cabc.Callable[..., WebSocketResource] | None = None,
) -> WebSocketRouter:
    """Return a mounted router that resolves to ``resource``."""
    router = WebSocketRouter(resource_factory=resource_factory)
    router.add_route("/session", lambda: resource)
    router.mount("/")
    return router


@pytest.mark.asyncio
async def test_router_dispatches_scripted_text_and_binary_frames_in_order() -> None:
    """One session dispatches mixed frames and sends replies on its socket."""
    resource = SessionResource()
    SessionResource.failures.clear()
    frames = [
        msjson.encode(Ping(payload="first")).decode("utf-8"),
        msjson.encode(Ping(payload="second")),
        msjson.encode(Ping(payload="third")).decode("utf-8"),
    ]
    ws = RecordingWS(frames, disconnect_code=1008)

    await _router_for(resource).on_websocket(make_req("/session"), ws)

    expected_replies = [
        {"type": "reply", "payload": "first"},
        {"type": "reply", "payload": "second"},
        {"type": "reply", "payload": "third"},
    ]
    assert resource.calls == ["first", "second", "third"], (
        "the router must dispatch every frame in arrival order"
    )
    assert ws.sent == expected_replies, (
        "handlers must send replies through the accepted socket"
    )
    assert resource.disconnects == [1008], "the peer close code must reach the resource"
    assert not ws.closed, "a peer disconnect must not be closed a second time"


@pytest.mark.asyncio
async def test_unknown_and_malformed_frames_keep_the_session_open() -> None:
    """Fallback handling does not prevent later valid messages from dispatching."""
    resource = SessionResource()
    unknown = b'{"type":"unknown","payload":"ignored"}'
    malformed = "not-json"
    ws = RecordingWS([
        unknown,
        malformed,
        msjson.encode(Ping(payload="after fallback")),
    ])

    await _router_for(resource).on_websocket(make_req("/session"), ws)

    assert resource.unhandled == [unknown, malformed], (
        "unknown tags and malformed JSON must reach on_unhandled as raw frames"
    )
    assert resource.calls == ["after fallback"], (
        "a healthy session must continue after on_unhandled returns"
    )
    sent_types = [typ.cast("dict[str, object]", message)["type"] for message in ws.sent]
    assert sent_types == ["error", "error", "reply"], (
        "fallback and ordinary responses must share the same WebSocket"
    )


@pytest.mark.asyncio
async def test_disconnect_runs_hooks_before_resource_cleanup_with_peer_code() -> None:
    """Normal disconnect cleanup receives the peer code in lifecycle order."""
    events: list[str] = []

    class RecordingDisconnectResource(SessionResource):
        async def on_disconnect(self, ws: WebSocketLike, close_code: int) -> None:
            events.append(f"resource:{close_code}")
            await super().on_disconnect(ws, close_code)

    resource = RecordingDisconnectResource()
    router = _router_for(resource)

    def before_disconnect(context: HookContext) -> None:
        events.append(f"hook:{context.close_code}")

    router.global_hooks.add("before_disconnect", before_disconnect)
    ws = RecordingWS(disconnect_code=4001)

    await router.on_websocket(make_req("/session"), ws)

    assert events == ["hook:4001", "resource:4001"], (
        "before_disconnect must run before on_disconnect"
    )


@pytest.mark.asyncio
async def test_handler_failure_closes_once_with_1011_and_reraises_original() -> None:
    """Handler failures close with 1011 and preserve the original exception."""
    resource = SessionResource()
    SessionResource.failures.clear()
    ws = RecordingWS([msjson.encode(Fail(payload="boom"))])

    with pytest.raises(RuntimeError, match="handler failed") as caught:
        await _router_for(resource).on_websocket(make_req("/session"), ws)

    assert caught.value is SessionResource.failures[-1], (
        "the router must propagate the same handler exception instance"
    )
    assert ws.closed == [1011], "the failed session must close exactly once with 1011"
    assert resource.disconnects == [1011], "failure cleanup must report code 1011"


@pytest.mark.asyncio
async def test_failure_cleanup_errors_do_not_replace_handler_error() -> None:
    """Disconnect-hook failures are best-effort during a failed session."""
    events: list[str] = []

    class BrokenDisconnectResource(SessionResource):
        async def on_disconnect(self, ws: WebSocketLike, close_code: int) -> None:
            del ws
            events.append(f"resource:{close_code}")
            await asyncio.sleep(0)
            raise ValueError

    resource = BrokenDisconnectResource()
    SessionResource.failures.clear()
    ws = RecordingWS([msjson.encode(Fail(payload="boom"))])

    def broken_before_disconnect(context: HookContext) -> None:
        events.append(f"hook:{context.close_code}")
        raise LookupError

    router = _router_for(resource)
    router.global_hooks.add("before_disconnect", broken_before_disconnect)

    with pytest.raises(RuntimeError, match="handler failed") as caught:
        await router.on_websocket(make_req("/session"), ws)

    assert caught.value is SessionResource.failures[-1], (
        "cleanup failures must not replace the original handler exception"
    )
    assert events == ["hook:1011", "resource:1011"], (
        "both failure-cleanup steps must be attempted in order"
    )
    assert ws.closed == [1011], "cleanup failures must not cause a second close"


@pytest.mark.asyncio
async def test_cancellation_closes_with_1001_and_skips_after_receive() -> None:
    """Cancellation cleans up the session without emitting after_receive."""

    class WaitingResource(SessionResource):
        hooks = HookCollection()

        def __init__(self) -> None:
            super().__init__()
            self.entered = asyncio.Event()
            self.release = asyncio.Event()

        @handles_message("ping")
        async def handle_ping(self, ws: WebSocketLike, payload: Ping) -> None:
            self.entered.set()
            await self.release.wait()
            await self._respond_to_ping(ws, payload)

    resource = WaitingResource()
    after_receive: list[str] = []
    router = _router_for(resource)
    router.global_hooks.add(
        "after_receive", lambda context: after_receive.append(context.event)
    )
    ws = RecordingWS([msjson.encode(Ping(payload="wait"))])
    task = asyncio.create_task(router.on_websocket(make_req("/session"), ws))
    try:
        await asyncio.wait_for(resource.entered.wait(), timeout=1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        resource.release.set()

    assert ws.closed == [1001], "cancellation must close with 1001"
    assert resource.disconnects == [1001], "cancellation cleanup must report 1001"
    assert not after_receive, "cancelled dispatch must skip after_receive"


@pytest.mark.asyncio
async def test_application_close_code_is_used_for_disconnect_cleanup() -> None:
    """An application close ends the receive loop with its chosen code."""

    class ClosingResource(SessionResource):
        @handles_message("ping")
        async def handle_ping(self, ws: WebSocketLike, payload: Ping) -> None:
            self.calls.append(payload.payload)
            await ws.close(4002)

    resource = ClosingResource()
    ws = RecordingWS([
        msjson.encode(Ping(payload="close")),
        msjson.encode(Ping(payload="must remain unread")),
    ])

    await _router_for(resource).on_websocket(make_req("/session"), ws)

    assert ws.closed == [4002], "the application must choose the close code"
    assert resource.disconnects == [4002], "on_disconnect must receive the app code"
    assert resource.calls == ["close"], "closed sockets must stop scripted receives"


@pytest.mark.asyncio
async def test_rejected_connection_does_not_start_session_or_disconnect() -> None:
    """A rejected handshake closes without receiving or disconnect cleanup."""

    class RejectingResource(SessionResource):
        async def on_connect(
            self, req: falcon.Request, ws: WebSocketLike, **params: object
        ) -> bool:
            del req, ws, params
            return False

    resource = RejectingResource()
    ws = RecordingWS([msjson.encode(Ping(payload="must not dispatch"))])

    await _router_for(resource).on_websocket(make_req("/session"), ws)

    assert not ws.accepted, "a rejected connection must never be accepted"
    assert ws.closed == [1000], "the existing rejection close behaviour must remain"
    assert not resource.calls, "a rejected connection must not dispatch frames"
    assert not resource.disconnects, "rejection must not run on_disconnect"


@pytest.mark.asyncio
async def test_nested_session_keeps_di_hooks_and_shared_state() -> None:
    """A nested resource receives DI, shared state, and parent hook context."""
    events: list[str] = []
    dependency = object()
    instances: list[WebSocketResource] = []

    class ChildResource(WebSocketResource):
        schema = Ping
        hooks = HookCollection()

        def __init__(self, dependency: object) -> None:
            self.dependency = dependency
            instances.append(self)

        @handles_message("ping")
        async def handle_ping(self, ws: WebSocketLike, payload: Ping) -> None:
            self.state["child"] = payload.payload

        async def on_disconnect(self, ws: WebSocketLike, close_code: int) -> None:
            events.append(f"disconnect:{close_code}")

    class ParentResource(WebSocketResource):
        hooks = HookCollection()

        def __init__(self, dependency: object) -> None:
            self.dependency = dependency
            self.state["parent"] = "shared"
            self.add_subroute("/child", ChildResource)
            instances.append(self)

        def get_child_context(self) -> dict[str, object]:
            return {"state": self.state}

    ParentResource.hooks.add(
        "before_disconnect", lambda context: events.append("parent-hook")
    )
    ChildResource.hooks.add(
        "before_disconnect", lambda context: events.append("child-hook")
    )
    container = ServiceContainer()
    container.register("dependency", dependency)
    router = WebSocketRouter(resource_factory=container.create_resource)
    router.add_route("/parent", ParentResource)
    router.mount("/")
    router.global_hooks.add(
        "before_disconnect", lambda context: events.append("global-hook")
    )
    ws = RecordingWS([msjson.encode(Ping(payload="done"))], disconnect_code=4003)

    await router.on_websocket(make_req("/parent/child"), ws)

    parent, child = instances
    assert isinstance(parent, ParentResource), "the router must keep the parent"
    assert isinstance(child, ChildResource), "the router must keep the child"
    assert parent.dependency is dependency, "the factory must inject the parent"
    assert child.dependency is dependency, "the factory must inject the child"
    assert parent.state is child.state, "nested resources must share connection state"
    assert child.state == {"parent": "shared", "child": "done"}, (
        "the selected child must update the parent's state mapping"
    )
    assert events == [
        "global-hook",
        "parent-hook",
        "child-hook",
        "disconnect:4003",
    ], "disconnect hooks must retain the resolved parent-to-child order"


@pytest.mark.asyncio
async def test_session_does_not_create_tasks_per_frame() -> None:
    """Inline dispatch keeps task count constant across a long frame sequence."""
    resource = SessionResource()
    frames = [msjson.encode(Ping(payload=str(index))) for index in range(50)]
    ws = RecordingWS(frames)
    current_tasks = asyncio.all_tasks()

    await _router_for(resource).on_websocket(make_req("/session"), ws)

    assert asyncio.all_tasks() == current_tasks, (
        "the router must not create one task or other background task per frame"
    )
    assert resource.calls == [str(index) for index in range(50)], (
        "all frames must still be dispatched in order"
    )
