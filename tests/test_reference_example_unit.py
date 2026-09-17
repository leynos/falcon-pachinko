"""Unit tests for the full reference example support modules."""

from __future__ import annotations

import importlib
import re
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
from falcon_pachinko.di import ServiceContainer as ServiceContainerImpl
from falcon_pachinko.websocket import WebSocketConnectionManager
from tests._stubs import RecordingWebSocket, RequestStub

if typ.TYPE_CHECKING:  # pragma: no cover - typing helpers, string-only annotations
    import types

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
async def test_token_authenticator_rejects_unknown_workspace() -> None:
    """A workspace with no configured secret is refused, not treated as open."""
    authenticator = TokenAuthenticator({"atlas": "secret"})
    with pytest.raises(AuthenticationError):
        await authenticator.verify("unknown", token=None)


@pytest.mark.asyncio
async def test_token_authenticator_accepts_configured_secret() -> None:
    """A configured workspace still verifies when the token matches."""
    configured = "s3kr1t-fixture"
    authenticator = TokenAuthenticator({"atlas": configured})
    await authenticator.verify("atlas", token=configured)


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


def _import_example_server(module_name: str) -> types.ModuleType:
    """Import an example server module, skipping if its extras are absent.

    The random-status example imports aiosqlite, which ships in the optional
    ``examples`` extra rather than the dev group, so it is unavailable in a
    plain ``uv sync --group dev`` environment.

    Returns
    -------
    types.ModuleType
        The imported example server module.
    """
    if "random_status" in module_name:
        pytest.importorskip(
            "aiosqlite", reason="random-status example needs the examples extra"
        )
    return importlib.import_module(module_name)


@pytest.mark.parametrize(
    ("module_name", "resolver_owner"),
    [
        ("examples.reference_app.server", "reference app"),
        ("examples.random_status.server", "random-status example"),
    ],
    ids=["reference_app", "random_status"],
)
def test_resolve_as_returns_the_service_when_the_type_matches(
    module_name: str, resolver_owner: str
) -> None:
    """A registered service of the expected type is returned unchanged."""
    module = _import_example_server(module_name)
    container = ServiceContainerImpl()
    authenticator = TokenAuthenticator({"atlas": "secret"})
    container.register("auth", authenticator)

    resolved = module._resolve_as(container, "auth", TokenAuthenticator)

    assert resolved is authenticator, (
        f"{resolver_owner} must return the registered instance unchanged"
    )


@pytest.mark.parametrize(
    "module_name",
    ["examples.reference_app.server", "examples.random_status.server"],
    ids=["reference_app", "random_status"],
)
def test_resolve_as_rejects_a_service_of_the_wrong_type(module_name: str) -> None:
    """A mismatched service raises TypeError naming the service and type."""
    module = _import_example_server(module_name)
    container = ServiceContainerImpl()
    container.register("auth", "not-an-authenticator")

    with pytest.raises(
        TypeError,
        match=re.escape("service 'auth' is not a TokenAuthenticator"),
    ):
        module._resolve_as(container, "auth", TokenAuthenticator)
