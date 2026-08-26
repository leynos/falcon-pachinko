"""Unit tests for the full reference example support modules."""

from __future__ import annotations

import typing as typ

import pytest
from falcon import HTTPUnauthorized

from examples.reference_app import build_container, build_router
from examples.reference_app.services import (
    AnnouncementFeed,
    AuthenticationError,
    Task,
    TaskCreationParams,
    TokenAuthenticator,
    WorkspaceRepository,
)
from falcon_pachinko.websocket import WebSocketConnectionManager
from tests._stubs import RecordingWebSocket, RequestStub

if typ.TYPE_CHECKING:  # pragma: no cover - typing helpers, string-only annotations
    from falcon_pachinko import ServiceContainer, WebSocketRouter

_TASKS_PATH = "/ws/workspaces/atlas/projects/triage/tasks"


def _build_router() -> tuple[WebSocketRouter, ServiceContainer]:
    conn_mgr = WebSocketConnectionManager()
    container = build_container(conn_mgr)
    router = build_router(container)
    return router, container


@pytest.mark.asyncio
async def test_router_rejects_missing_token() -> None:
    """Global hooks close connections that omit the workspace token."""
    router, _ = _build_router()
    req = RequestStub(_TASKS_PATH, headers={})
    ws = RecordingWebSocket()
    with pytest.raises(HTTPUnauthorized):
        await router.on_websocket(req, ws)
    assert ws.closed is True, "the connection must be closed without a token"
    assert ws.accepted is False, "the connection must not be accepted"


@pytest.mark.asyncio
async def test_router_accepts_with_valid_token() -> None:
    """Connections presenting the correct headers are accepted."""
    router, _ = _build_router()
    req = RequestStub(
        _TASKS_PATH,
        headers={"x-workspace-token": "seekrit", "x-user": "riley"},
    )
    ws = RecordingWebSocket()
    await router.on_websocket(req, ws)
    assert ws.accepted is True, "a valid token must accept the connection"
    assert ws.messages, "the resource must send a session-ready message"
    first = ws.messages[0]
    assert isinstance(first, dict), "the first outbound message must be a mapping"
    assert first["type"] == "session.ready", (
        "the first outbound message must be a session.ready event"
    )


@pytest.mark.asyncio
async def test_workspace_repository_task_lifecycle() -> None:
    """Tasks can be created, assigned, and completed within a project."""
    repo = WorkspaceRepository()
    await repo.add_task(
        "atlas",
        "triage",
        TaskCreationParams(
            task_id="T-1",
            title="Investigate outage",
            author="avery",
            assignee="brooke",
        ),
    )
    task = await repo.assign_task("atlas", "triage", "T-1", "casey")
    assert task.assigned_to == "casey", "the task must be assigned to casey"
    task = await repo.complete_task("atlas", "triage", "T-1")
    assert task.completed is True, "the task must be marked completed"
    tasks = await repo.list_tasks("atlas", "triage", include_completed=False)
    assert tasks == [], "completed tasks must be excluded when requested"
    tasks = await repo.list_tasks("atlas", "triage", include_completed=True)
    assert isinstance(tasks[0], Task), "the listed item must be a Task"
    assert tasks[0].completed is True, (
        "the completed task must be included when requested"
    )


@pytest.mark.asyncio
async def test_token_authenticator_rejects_invalid_secret() -> None:
    """Connections presenting the wrong token raise ``AuthenticationError``."""
    authenticator = TokenAuthenticator({"atlas": "secret"})
    with pytest.raises(AuthenticationError):
        # ruff: ignore[hardcoded-password-func-arg] -- deliberately wrong test token
        await authenticator.verify("atlas", token="nope")


@pytest.mark.asyncio
async def test_token_authenticator_allows_unknown_workspace() -> None:
    """Workspaces without a configured secret verify without raising."""
    authenticator = TokenAuthenticator({"atlas": "secret"})
    await authenticator.verify("unknown", token=None)


@pytest.mark.asyncio
async def test_announcement_feed_preserves_order() -> None:
    """Announcement feed publishes events FIFO for the worker."""
    feed = AnnouncementFeed()
    await feed.publish("atlas", {"type": "a"})
    await feed.publish("atlas", {"type": "b"})
    first = await feed.next_event()
    second = await feed.next_event()
    assert first == ("atlas", {"type": "a"}), (
        "the first published event must come first"
    )
    assert second == ("atlas", {"type": "b"}), (
        "the second published event must follow the first"
    )
