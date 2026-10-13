"""Tests for Falcon-attached WebSocket router lifecycle behaviour."""

from __future__ import annotations

import asyncio
import json

import falcon
import falcon.asgi
import falcon.testing
import msgspec as ms
import pytest

from falcon_pachinko import (
    WebSocketLike,
    WebSocketResource,
    WebSocketRouter,
    handles_message,
)
from falcon_pachinko.router import _AcceptedConnection
from falcon_pachinko.unittests.helpers import RecordingWS, make_req


class _AcceptingResource(WebSocketResource):
    """Resource that always accepts a connection."""

    async def on_connect(self, req: object, ws: object, **params: object) -> bool:
        return True


class _LiveTaskMessage(ms.Struct, tag="task.add"):
    """Task message decoded by the live Falcon responder test."""

    task_id: str


class _LiveTaskResource(WebSocketResource):
    """Final nested resource used to exercise the responder lifecycle."""

    schema = _LiveTaskMessage

    def __init__(self, events: list[str]) -> None:
        self.events = events

    async def on_connect(self, req: object, ws: object, **params: object) -> bool:
        self.events.append(f"connected:{params['workspace_id']}:{params['project_id']}")
        return True

    async def on_accepted(self, ws: WebSocketLike) -> None:
        await ws.send_media({
            "type": "session.ready",
            "payload": {"workspace": "atlas", "project": "triage"},
        })

    @handles_message("task.add")
    async def handle_add(self, ws: WebSocketLike, payload: _LiveTaskMessage) -> None:
        self.events.append(f"handled:{payload.task_id}")
        await ws.send_media({"type": "task.added", "payload": payload.task_id})

    async def on_disconnect(self, ws: object, close_code: int) -> None:
        self.events.append(f"disconnected:{close_code}")


class _LiveProjectResource(WebSocketResource):
    """Parent resource that mounts the task stream below a project."""

    def __init__(self, events: list[str]) -> None:
        self.add_subroute(
            "projects/{project_id}/tasks", _LiveTaskResource, args=(events,)
        )


class _RejectingResource(WebSocketResource):
    """Resource that rejects a connection before a receive loop can start."""

    async def on_connect(self, req: object, ws: object, **params: object) -> bool:
        return False


class _LifecycleResource(WebSocketResource):
    """Resource for accepted setup, handler failures, and disconnect tests."""

    def __init__(
        self,
        events: list[str],
        *,
        fail_accepted: bool = False,
        fail_handler: bool = False,
    ) -> None:
        self.events = events
        self.fail_accepted = fail_accepted
        self.fail_handler = fail_handler

    async def on_connect(self, req: object, ws: object, **params: object) -> bool:
        return True

    async def on_accepted(self, ws: object) -> None:
        self.events.append("accepted")
        if self.fail_accepted:
            raise RuntimeError

    async def on_unhandled(self, ws: object, message: str | bytes) -> None:
        if self.fail_handler:
            raise RuntimeError

    async def on_disconnect(self, ws: object, close_code: int) -> None:
        self.events.append(f"disconnected:{close_code}")


class _DisconnectingAcceptedResource(WebSocketResource):
    """Resource whose initial send observes a normal peer disconnect."""

    def __init__(self, events: list[str]) -> None:
        self.events = events

    async def on_connect(self, req: object, ws: object, **params: object) -> bool:
        return True

    async def on_accepted(self, ws: object) -> None:
        raise falcon.WebSocketDisconnected(code=4007)

    async def on_disconnect(self, ws: object, close_code: int) -> None:
        self.events.append(f"disconnected:{close_code}")


class _ScriptedTextWebSocket(RecordingWS):
    """Return scripted text frames or raise a scripted receive outcome."""

    def __init__(self, outcomes: list[str | BaseException]) -> None:
        super().__init__()
        self.outcomes = list(outcomes)
        self.receive_calls = 0

    async def receive_text(self) -> str:
        self.receive_calls += 1
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


@pytest.mark.asyncio
async def test_uri_template_fallback_strips_terminal_greedy_path_field() -> None:
    """The router accepts Falcon-style URI templates without path_template."""
    router = WebSocketRouter()
    router.add_route("/ok", _AcceptingResource)
    router.mount("/api")
    req = type(
        "URIRequest",
        (),
        {"path": "/api/ok", "uri_template": "/api/{_pachinko_path:path}"},
    )()

    ws = RecordingWS()
    await router.on_websocket(req, ws)

    assert ws.accepted == [None], "URI-template fallback should accept /api/ok"


def test_attach_requires_a_mounted_router() -> None:
    """Falcon routes cannot be attached before the router has a prefix."""
    router = WebSocketRouter()

    with pytest.raises(RuntimeError, match="mount"):
        router.attach(falcon.asgi.App())


def test_attach_rejects_a_second_attachment() -> None:
    """A router has one Falcon responder attachment for its mounted prefix."""
    router = WebSocketRouter()
    router.mount("/ws")
    app = falcon.asgi.App()
    router.attach(app)

    with pytest.raises(RuntimeError, match="attached"):
        router.attach(app)


@pytest.mark.asyncio
async def test_attached_router_dispatches_nested_schema_frames_and_finalizes() -> None:
    """Falcon drives nested routing, dispatch, hooks, replies, and disconnect."""
    events: list[str] = []
    router = WebSocketRouter()
    router.add_route("/workspaces/{workspace_id}", _LiveProjectResource, events)
    router.mount("/ws")
    router.global_hooks.add(
        "before_disconnect", lambda context: events.append(context.event)
    )
    app = falcon.asgi.App()
    router.attach(app)

    async with (
        falcon.testing.ASGIConductor(app) as conductor,
        conductor.simulate_ws("/ws/workspaces/atlas/projects/triage/tasks") as ws,
    ):
        ready = json.loads(await asyncio.wait_for(ws.receive_text(), timeout=1))
        assert ready == {
            "type": "session.ready",
            "payload": {"workspace": "atlas", "project": "triage"},
        }, "the accepted nested resource must send its ready event"

        await ws.send_text('{"type":"task.add","task_id":"T-42"}')
        reply = json.loads(await asyncio.wait_for(ws.receive_text(), timeout=1))
        assert reply == {"type": "task.added", "payload": "T-42"}, (
            "the schema-decoded handler must return its task acknowledgement"
        )

    assert events == [
        "connected:atlas:triage",
        "handled:T-42",
        "before_disconnect",
        "disconnected:1000",
    ], "the router must dispatch frames and run disconnect hooks in order"


@pytest.mark.asyncio
async def test_attached_router_avoids_a_mount_capture_name_collision() -> None:
    """The private greedy capture stays distinct from mounted URI fields."""
    router = WebSocketRouter()
    router.add_route("/rooms", _AcceptingResource)
    router.mount("/{_falcon_pachinko_path}")
    app = falcon.asgi.App()
    router.attach(app)

    async with (
        falcon.testing.ASGIConductor(app) as conductor,
        conductor.simulate_ws("/tenant/rooms"),
    ):
        pass


@pytest.mark.asyncio
async def test_attached_router_rejection_does_not_start_a_receive_loop() -> None:
    """A rejected connection closes before the adapter attempts to receive."""
    router = WebSocketRouter()
    router.add_route("/denied", _RejectingResource)
    router.mount("/ws")
    app = falcon.asgi.App()
    router.attach(app)

    async with falcon.testing.ASGIConductor(app) as conductor:
        with pytest.raises(falcon.WebSocketDisconnected):
            async with conductor.simulate_ws("/ws/denied"):
                pytest.fail("a rejected handshake must not enter the context")


@pytest.mark.asyncio
async def test_attached_router_supports_a_root_mount() -> None:
    """A root-mounted router registers both exact and nested routes."""
    router = WebSocketRouter()
    router.add_route("/rooms", _AcceptingResource)
    router.mount("/")
    app = falcon.asgi.App()
    router.attach(app)

    async with (
        falcon.testing.ASGIConductor(app) as conductor,
        conductor.simulate_ws("/rooms"),
    ):
        pass


@pytest.mark.asyncio
async def test_direct_on_websocket_sets_up_without_receiving_frames() -> None:
    """The direct simulator entry point remains setup-only."""
    events: list[str] = []
    router = WebSocketRouter()
    router.add_route("/test", _LifecycleResource, events)
    router.mount("/")
    ws = _ScriptedTextWebSocket(["{}"])

    await router.on_websocket(make_req("/test", "/"), ws)

    assert ws.receive_calls == 0, "direct router setup must leave receiving to callers"
    assert events == ["accepted"], "setup should run the post-accept hook once"


@pytest.mark.asyncio
async def test_on_accepted_failure_runs_disconnect_finalization() -> None:
    """Post-accept setup failures close with 1011 and still clean up."""
    events: list[str] = []
    router = WebSocketRouter()
    router.global_hooks.add(
        "before_disconnect",
        lambda context: events.append(f"before:{context.close_code}"),
    )
    router.add_route("/test", _LifecycleResource, events, fail_accepted=True)
    router.mount("/")
    ws = RecordingWS()

    with pytest.raises(RuntimeError):
        await router.on_websocket(make_req("/test", "/"), ws)

    assert ws.accepted == [None], "the handshake must be accepted before setup fails"
    assert ws.closed == [1011], "failed accepted setup must close with code 1011"
    assert events == [
        "accepted",
        "before:1011",
        "disconnected:1011",
    ], "disconnect hooks must precede resource cleanup after setup failure"


@pytest.mark.asyncio
async def test_on_accepted_peer_disconnect_uses_the_peer_close_code() -> None:
    """A peer disconnect during accepted setup completes as normal cleanup."""
    events: list[str] = []
    router = WebSocketRouter()
    router.global_hooks.add(
        "before_disconnect",
        lambda context: events.append(f"before:{context.close_code}"),
    )
    router.add_route("/test", _DisconnectingAcceptedResource, events)
    router.mount("/")
    ws = RecordingWS()

    await router.on_websocket(make_req("/test", "/"), ws)

    assert ws.accepted == [None], "the handshake must precede accepted setup"
    assert not ws.closed, "normal peer disconnect must not trigger another close"
    assert events == ["before:4007", "disconnected:4007"], (
        "disconnect callbacks must retain the peer's close code and order"
    )


def _accepted_lifecycle_connection(
    events: list[str],
    ws: _ScriptedTextWebSocket,
    *,
    fail_handler: bool = False,
) -> tuple[WebSocketRouter, _AcceptedConnection]:
    """Build the accepted router state consumed by scripted receive tests."""
    router = WebSocketRouter()
    router.global_hooks.add(
        "before_disconnect",
        lambda context: events.append(f"before:{context.close_code}"),
    )
    resource = _LifecycleResource(events, fail_handler=fail_handler)
    manager = router._setup_hook_management([resource])
    return router, _AcceptedConnection(resource, ws, manager)


@pytest.mark.asyncio
async def test_receive_loop_uses_peer_code_without_closing_again() -> None:
    """A peer disconnect's code reaches hooks and resource cleanup unchanged."""
    events: list[str] = []
    ws = _ScriptedTextWebSocket([falcon.WebSocketDisconnected(code=4001)])
    router, connection = _accepted_lifecycle_connection(events, ws)

    await router._drive_accepted_connection(connection)

    assert events == ["before:4001", "disconnected:4001"], (
        "the peer close code must be shared after before-disconnect hooks"
    )
    assert not ws.closed, "normal peer disconnect must not trigger another close"


@pytest.mark.asyncio
async def test_handler_error_closes_with_1011_and_is_reraised() -> None:
    """Unexpected handler errors close the socket and retain their exception."""
    events: list[str] = []
    ws = _ScriptedTextWebSocket(["{}"])
    router, connection = _accepted_lifecycle_connection(events, ws, fail_handler=True)

    with pytest.raises(RuntimeError):
        await router._drive_accepted_connection(connection)

    assert ws.closed == [1011], "handler errors must close with code 1011"
    assert events == ["before:1011", "disconnected:1011"], (
        "handler failures must still run disconnect finalization in order"
    )


@pytest.mark.asyncio
async def test_cancellation_finalizes_with_1006_and_is_reraised() -> None:
    """Cancellation uses abnormal close metadata and is propagated."""
    events: list[str] = []
    ws = _ScriptedTextWebSocket([asyncio.CancelledError()])
    router, connection = _accepted_lifecycle_connection(events, ws)

    with pytest.raises(asyncio.CancelledError):
        await router._drive_accepted_connection(connection)

    assert not ws.closed, "cancellation must not send reserved code 1006"
    assert events == ["before:1006", "disconnected:1006"], (
        "cancellation cleanup must run with code 1006 before it is re-raised"
    )
