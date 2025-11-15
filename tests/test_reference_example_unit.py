"""Unit tests for the full reference example support modules."""

from __future__ import annotations

import typing as typ

import falcon
import pytest
from falcon import HTTPUnauthorized

from examples.reference_app import build_container, build_router
from examples.reference_app.services import (
    AnnouncementFeed,
    AuthenticationError,
    Task,
    TokenAuthenticator,
    WorkspaceRepository,
)
from falcon_pachinko.protocols import WebSocketLike
from falcon_pachinko.websocket import WebSocketConnectionManager

if typ.TYPE_CHECKING:  # pragma: no cover - typing helpers
    from falcon_pachinko import ServiceContainer, WebSocketRouter
else:  # pragma: no cover - runtime stubs for annotations
    ServiceContainer = typ.Any  # type: ignore[assignment]
    WebSocketRouter = typ.Any  # type: ignore[assignment]


class _RequestStub:
    def __init__(self, headers: dict[str, str]) -> None:
        self.path = "/ws/workspaces/atlas/projects/triage/tasks"
        self.path_template = "/ws"
        self._headers = {key.lower(): value for key, value in headers.items()}

    def get_header(self, name: str, default: str | None = None) -> str | None:
        return self._headers.get(name.lower(), default)


class _WebSocketStub(WebSocketLike):
    def __init__(self) -> None:
        self.accepted = False
        self.closed = False
        self.close_code: int | None = None
        self.messages: list[object] = []

    async def accept(self, subprotocol: str | None = None) -> None:
        self.accepted = True

    async def close(self, code: int = 1000) -> None:
        self.closed = True
        self.close_code = code

    async def send_media(self, data: object) -> None:
        self.messages.append(data)

    async def receive_media(self) -> object:
        return None


def _build_router() -> tuple[WebSocketRouter, ServiceContainer]:
    conn_mgr = WebSocketConnectionManager()
    container = build_container(conn_mgr)
    router = build_router(container)
    return router, container


@pytest.mark.asyncio
async def test_router_rejects_missing_token() -> None:
    """Global hooks close connections that omit the workspace token."""
    router, _ = _build_router()
    req = _RequestStub(headers={})
    ws = _WebSocketStub()
    with pytest.raises(HTTPUnauthorized):
        await router.on_websocket(
            typ.cast("falcon.Request", req),
            ws,
        )
    assert ws.closed is True
    assert ws.accepted is False


@pytest.mark.asyncio
async def test_router_accepts_with_valid_token() -> None:
    """Connections presenting the correct headers are accepted."""
    router, _ = _build_router()
    req = _RequestStub(headers={"x-workspace-token": "seekrit", "x-user": "riley"})
    ws = _WebSocketStub()
    await router.on_websocket(typ.cast("falcon.Request", req), ws)
    assert ws.accepted is True
    assert ws.messages
    first = typ.cast("dict[str, object]", ws.messages[0])
    assert first["type"] == "session.ready"


@pytest.mark.asyncio
async def test_workspace_repository_task_lifecycle() -> None:
    """Tasks can be created, assigned, and completed within a project."""
    repo = WorkspaceRepository()
    await repo.add_task(
        "atlas",
        "triage",
        task_id="T-1",
        title="Investigate outage",
        author="avery",
        assignee="brooke",
    )
    task = await repo.assign_task("atlas", "triage", "T-1", "casey")
    assert task.assigned_to == "casey"
    task = await repo.complete_task("atlas", "triage", "T-1")
    assert task.completed is True
    tasks = await repo.list_tasks("atlas", "triage", include_completed=False)
    assert tasks == []
    tasks = await repo.list_tasks("atlas", "triage", include_completed=True)
    assert isinstance(tasks[0], Task)
    assert tasks[0].completed is True


@pytest.mark.asyncio
async def test_token_authenticator_rejects_invalid_secret() -> None:
    """Connections presenting the wrong token raise ``AuthenticationError``."""
    authenticator = TokenAuthenticator({"atlas": "secret"})
    with pytest.raises(AuthenticationError):
        await authenticator.verify("atlas", token="nope")  # noqa: S106
    await authenticator.verify("unknown", token=None)


@pytest.mark.asyncio
async def test_announcement_feed_preserves_order() -> None:
    """Announcement feed publishes events FIFO for the worker."""
    feed = AnnouncementFeed()
    await feed.publish("atlas", {"type": "a"})
    await feed.publish("atlas", {"type": "b"})
    first = await feed.next_event()
    second = await feed.next_event()
    assert first == ("atlas", {"type": "a"})
    assert second == ("atlas", {"type": "b"})
