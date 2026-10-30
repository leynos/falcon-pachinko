"""Behavioural unit tests for ``TaskStreamResource`` message handlers."""

from __future__ import annotations

import pytest

from examples.reference_app.resources import (
    AssignTask,
    BroadcastNote,
    CompleteTask,
    ListTasks,
    TaskStreamResource,
)
from examples.reference_app.services import (
    TaskCreationParams,
    WorkspaceRepository,
)
from tests._reference_example_support import (
    _PROJECT,
    _WORKSPACE,
    _build_resource,
    _dispatch,
)
from tests._stubs import RecordingWebSocket


async def _resource_with_task(
    params: TaskCreationParams,
) -> tuple[TaskStreamResource, WorkspaceRepository, RecordingWebSocket]:
    """Return a resource bound to the test project, holding one added task."""
    resource, repo, _audit, _feed = _build_resource()
    resource.state["workspace_id"] = _WORKSPACE
    resource.state["project_id"] = _PROJECT
    await repo.add_task(_WORKSPACE, _PROJECT, params)
    return resource, repo, RecordingWebSocket()


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
