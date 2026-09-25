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
async def test_handle_complete_marks_the_task_and_replies() -> None:
    """Completing a task updates the repository and replies with its state."""
    resource, repo, _audit, _feed = _build_resource()
    resource.state["workspace_id"] = _WORKSPACE
    resource.state["project_id"] = _PROJECT
    await repo.add_task(
        _WORKSPACE,
        _PROJECT,
        TaskCreationParams(task_id="T-1", title="Fix it", author="avery"),
    )
    ws = RecordingWebSocket()

    await _dispatch(resource, ws, CompleteTask(task_id="T-1"))

    assert ws.messages == [
        {"type": "task.completed", "payload": {"task_id": "T-1", "completed": True}}
    ], f"the completion reply must echo the task state: {ws.messages}"


@pytest.mark.asyncio
async def test_handle_assign_reassigns_the_task_and_replies() -> None:
    """Assigning a task updates the assignee and replies with the new owner."""
    resource, repo, _audit, _feed = _build_resource()
    resource.state["workspace_id"] = _WORKSPACE
    resource.state["project_id"] = _PROJECT
    await repo.add_task(
        _WORKSPACE,
        _PROJECT,
        TaskCreationParams(task_id="T-2", title="Ship it", author="avery"),
    )
    ws = RecordingWebSocket()

    await _dispatch(resource, ws, AssignTask(task_id="T-2", assignee="casey"))

    assert ws.messages == [
        {"type": "task.assigned", "payload": {"task_id": "T-2", "assignee": "casey"}}
    ], f"the assignment reply must echo the new assignee: {ws.messages}"


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
