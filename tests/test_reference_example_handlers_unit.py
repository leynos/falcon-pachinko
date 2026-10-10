"""Behavioural unit tests for ``TaskStreamResource`` handlers and lifecycle."""

from __future__ import annotations

import typing as typ

import msgspec.json as msjson
import pytest

from examples.reference_app.resources import (
    AssignTask,
    BroadcastNote,
    CompleteTask,
    ListTasks,
    TaskStreamResource,
)
from examples.reference_app.services import (
    AnnouncementFeed,
    AuditTrail,
    TaskCreationParams,
    WorkspaceRepository,
)
from falcon_pachinko.websocket import InProcessBackend, WebSocketConnectionManager
from tests._stubs import RecordingWebSocket, RequestStub

if typ.TYPE_CHECKING:
    import falcon

    from falcon_pachinko.protocols import WebSocketLike

_WORKSPACE = "atlas"
_PROJECT = "triage"


class FailingJoinBackend(InProcessBackend):
    """In-process backend whose ``join_room`` always fails.

    Everything else is the real backend, so the connection the resource adds
    before joining is genuinely registered and genuinely removed on cleanup.
    """

    async def join_room(self, conn_id: str, room: str) -> None:
        """Raise, simulating a broken room-membership backend."""
        msg = f"cannot join {room!r}"
        raise RuntimeError(msg)


def _build_resource(
    conn_mgr: WebSocketConnectionManager | None = None,
) -> tuple[TaskStreamResource, WorkspaceRepository, AuditTrail, AnnouncementFeed]:
    """Construct a ``TaskStreamResource`` with fresh, real collaborators."""
    repo = WorkspaceRepository()
    audit = AuditTrail()
    feed = AnnouncementFeed()
    resource = TaskStreamResource(
        workspace_repo=repo,
        audit_trail=audit,
        announcement_feed=feed,
        conn_mgr=conn_mgr or WebSocketConnectionManager(),
    )
    return resource, repo, audit, feed


def _connect_request() -> falcon.Request:
    """Return a request double carrying the reference app's user header."""
    stub = RequestStub(
        "/ws/workspaces/atlas/projects/triage/tasks",
        headers={"x-user": "riley"},
    )
    return typ.cast("falcon.Request", stub)


async def _resource_with_task(
    params: TaskCreationParams,
) -> tuple[TaskStreamResource, WorkspaceRepository, RecordingWebSocket]:
    """Return a resource bound to the test project, holding one added task."""
    resource, repo, _audit, _feed = _build_resource()
    resource.state["workspace_id"] = _WORKSPACE
    resource.state["project_id"] = _PROJECT
    await repo.add_task(_WORKSPACE, _PROJECT, params)
    return resource, repo, RecordingWebSocket()


async def _dispatch(
    resource: TaskStreamResource, ws: WebSocketLike, payload: object
) -> None:
    """Bind a standalone hook manager and dispatch an encoded ``payload``."""
    resource.bind_default_hook_manager()
    await resource.dispatch(ws, msjson.encode(payload))


@pytest.mark.asyncio
async def test_on_connect_cleans_up_and_reraises_when_join_room_fails() -> None:
    """A failed room join removes the connection and re-raises the error."""
    conn_mgr = WebSocketConnectionManager(backend=FailingJoinBackend())
    resource, _repo, _audit, _feed = _build_resource(conn_mgr)
    ws = RecordingWebSocket()

    with pytest.raises(RuntimeError, match="cannot join"):
        await resource.on_connect(
            _connect_request(), ws, workspace_id=_WORKSPACE, project_id=_PROJECT
        )

    remaining = conn_mgr.websockets
    assert remaining == {}, f"the failed connection must be removed: {remaining}"
    assert not ws.messages, "no session.ready message is sent on failure"


@pytest.mark.asyncio
async def test_on_disconnect_is_a_noop_when_never_connected() -> None:
    """Disconnecting a resource that never connected does nothing."""
    resource, _repo, audit, _feed = _build_resource()

    await resource.on_disconnect(RecordingWebSocket(), close_code=1000)

    assert not audit.records, "no audit record is written without a connection"


@pytest.mark.asyncio
async def test_on_disconnect_removes_connection_and_audits_closure() -> None:
    """Disconnecting a connected resource removes it and audits the closure."""
    conn_mgr = WebSocketConnectionManager()
    resource, _repo, audit, _feed = _build_resource(conn_mgr)
    ws = RecordingWebSocket()
    await resource.on_connect(
        _connect_request(), ws, workspace_id=_WORKSPACE, project_id=_PROJECT
    )

    await resource.on_disconnect(ws, close_code=1001)

    assert conn_mgr.websockets == {}, "the connection must be removed on disconnect"
    closed = [r for r in audit.records if r["event"] == "session.closed"]
    assert len(closed) == 1, f"exactly one session.closed record is expected: {closed}"
    metadata = closed[0]["metadata"]
    assert isinstance(metadata, dict), "audit metadata must be a mapping"
    assert metadata["close_code"] == 1001, "close_code must match"
    assert metadata["workspace"] == _WORKSPACE, "workspace must match"
    assert metadata["project"] == _PROJECT, "project must match"


@pytest.mark.asyncio
@pytest.mark.parametrize("tag", ["disconnect", "Disconnect", "DISCONNECT"])
async def test_reserved_envelope_tag_leaves_live_membership_intact(tag: str) -> None:
    """A ``disconnect`` envelope cannot evict a live connection.

    The payload is a valid close code, so nothing but the reserved-name
    refusal stops ``on_disconnect`` from tearing down a connected session.
    """
    conn_mgr = WebSocketConnectionManager()
    resource, _repo, audit, _feed = _build_resource(conn_mgr)
    ws = RecordingWebSocket()
    await resource.on_connect(
        _connect_request(), ws, workspace_id=_WORKSPACE, project_id=_PROJECT
    )

    await _dispatch(resource, ws, {"type": tag, "payload": 1000})

    assert dict(conn_mgr.websockets) == {resource._conn_id: ws}, (
        f"the live connection must survive a reserved envelope: {conn_mgr.websockets}"
    )
    assert dict(conn_mgr.rooms) == {f"workspace:{_WORKSPACE}": {resource._conn_id}}, (
        f"workspace membership must be unchanged: {conn_mgr.rooms}"
    )
    assert not [r for r in audit.records if r["event"] == "session.closed"], (
        "a reserved envelope must not audit a closure"
    )
    assert ws.messages[-1] == {
        "type": "error",
        "payload": "unsupported message",
    }, f"the application fallback must answer the reserved envelope: {ws.messages}"


class EnvelopeTaskStreamResource(TaskStreamResource):
    """``TaskStreamResource`` without a schema, so envelopes are decoded.

    The reference deployment compiles its inbound payloads before they reach
    the WebSocket loop. A deployment that skips that compilation still hands
    the same class to ``dispatch()``, and the envelope path is then peer
    steerable. This subclass keeps the real lifecycle and collaborators while
    forcing that path.
    """

    schema = None


@pytest.mark.asyncio
async def test_reserved_envelope_cannot_evict_through_real_lifecycle() -> None:
    """A ``disconnect`` envelope cannot reach the real cleanup callback.

    The close code is type-correct for ``on_disconnect``, so only the
    reserved-name refusal stands between the frame and a live-connection
    teardown.
    """
    conn_mgr = WebSocketConnectionManager()
    resource, _repo, audit, _feed = _build_resource(conn_mgr)
    envelope_resource = EnvelopeTaskStreamResource(
        workspace_repo=resource._repo,
        audit_trail=resource._audit,
        announcement_feed=resource._feed,
        conn_mgr=conn_mgr,
    )
    ws = RecordingWebSocket()
    await envelope_resource.on_connect(
        _connect_request(), ws, workspace_id=_WORKSPACE, project_id=_PROJECT
    )
    conn_id = envelope_resource._conn_id

    await _dispatch(envelope_resource, ws, {"type": "disconnect", "payload": 1000})

    assert dict(conn_mgr.websockets) == {conn_id: ws}, (
        f"the live connection must survive: {conn_mgr.websockets}"
    )
    assert dict(conn_mgr.rooms) == {f"workspace:{_WORKSPACE}": {conn_id}}, (
        f"workspace membership must be unchanged: {conn_mgr.rooms}"
    )
    assert not [r for r in audit.records if r["event"] == "session.closed"], (
        "the reserved envelope must not audit a closure"
    )
    assert ws.messages[-1] == {
        "type": "error",
        "payload": "unsupported message",
    }, f"the application fallback must answer the frame: {ws.messages}"

    # The session must still be usable afterwards, not merely reported live.
    await _dispatch(envelope_resource, ws, {"type": "task.list", "payload": {}})
    last = ws.messages[-1]
    assert isinstance(last, dict) and last["type"] == "task.list", (
        f"a genuine message must still be served: {ws.messages}"
    )


@pytest.mark.asyncio
async def test_connect_envelope_tag_does_not_reconnect() -> None:
    """A ``connect`` envelope cannot re-run the connect lifecycle."""
    conn_mgr = WebSocketConnectionManager()
    resource, _repo, _audit, _feed = _build_resource(conn_mgr)
    ws = RecordingWebSocket()
    await resource.on_connect(
        _connect_request(), ws, workspace_id=_WORKSPACE, project_id=_PROJECT
    )
    ready_count = len(ws.messages)

    await _dispatch(resource, ws, {"type": "connect", "payload": True})

    assert len(conn_mgr.websockets) == 1, (
        "connect must not register a second connection"
    )
    assert len(ws.messages) == ready_count + 1, (
        f"only the fallback reply is expected: {ws.messages}"
    )
    assert ws.messages[-1] == {
        "type": "error",
        "payload": "unsupported message",
    }, f"the connect tag must fall back, not send a second session.ready: {ws.messages}"


@pytest.mark.asyncio
async def test_handle_complete_marks_the_task_and_replies() -> None:
    """Completing a task updates the repository and replies with its state."""
    resource, repo, ws = await _resource_with_task(
        TaskCreationParams(task_id="T-1", title="Fix it", author="avery")
    )

    await _dispatch(resource, ws, CompleteTask(task_id="T-1"))

    assert ws.messages == [
        {"type": "task.completed", "payload": {"task_id": "T-1", "completed": True}}
    ], f"the completion reply must echo the task state: {ws.messages}"
    (stored,) = await repo.list_tasks(_WORKSPACE, _PROJECT, include_completed=True)
    assert stored.completed is True, "the repository must record the completion"


@pytest.mark.asyncio
async def test_handle_assign_reassigns_the_task_and_replies() -> None:
    """Assigning a task updates the assignee and replies with the new owner."""
    resource, repo, ws = await _resource_with_task(
        TaskCreationParams(task_id="T-2", title="Ship it", author="avery")
    )

    await _dispatch(resource, ws, AssignTask(task_id="T-2", assignee="casey"))

    assert ws.messages == [
        {"type": "task.assigned", "payload": {"task_id": "T-2", "assignee": "casey"}}
    ], f"the assignment reply must echo the new assignee: {ws.messages}"
    (stored,) = await repo.list_tasks(_WORKSPACE, _PROJECT, include_completed=True)
    assert stored.assigned_to == "casey", "the repository must record the assignee"


@pytest.mark.asyncio
async def test_handle_list_returns_only_incomplete_tasks_when_requested() -> None:
    """Listing tasks with ``include_completed=False`` filters completed ones."""
    resource, repo, _audit, _feed = _build_resource()
    resource.state["workspace_id"] = _WORKSPACE
    resource.state["project_id"] = _PROJECT
    await repo.add_task(
        _WORKSPACE,
        _PROJECT,
        TaskCreationParams(task_id="T-3", title="Open", author="avery"),
    )
    await repo.add_task(
        _WORKSPACE,
        _PROJECT,
        TaskCreationParams(task_id="T-4", title="Done", author="avery"),
    )
    await repo.complete_task(_WORKSPACE, _PROJECT, "T-4")
    ws = RecordingWebSocket()

    await _dispatch(resource, ws, ListTasks(include_completed=False))

    reply_count = len(ws.messages)
    assert reply_count == 1, f"expected one task.list reply, got {reply_count}"
    message = ws.messages[0]
    assert isinstance(message, dict), "the reply must be a mapping"
    assert message["type"] == "task.list", "the reply type must be task.list"
    task_ids = [task["task_id"] for task in message["payload"]]
    assert task_ids == ["T-3"], f"only the incomplete task must be listed: {task_ids}"


@pytest.mark.asyncio
async def test_handle_note_publishes_an_announcement_and_echoes_the_text() -> None:
    """A session note is published to the feed and echoed to the sender."""
    resource, _repo, _audit, feed = _build_resource()
    resource.state["workspace_id"] = _WORKSPACE
    ws = RecordingWebSocket()

    await _dispatch(resource, ws, BroadcastNote(text="all clear"))

    expected_reply = {"type": "session.note", "payload": "all clear"}
    assert ws.messages == [expected_reply], f"note not echoed: {ws.messages}"
    workspace_id, event = await feed.next_event()
    assert workspace_id == _WORKSPACE, "the announcement must target the workspace"
    assert event == {
        "type": "announcement",
        "payload": {"kind": "note", "text": "all clear"},
    }, f"the announcement must carry the note text: {event}"
