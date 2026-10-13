"""Live Falcon WebSocket acceptance scenarios for the reference application."""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses as dc
import typing as typ

import pytest
from pytest_bdd import given, scenario, then, when
from websockets.exceptions import InvalidStatusCode

from examples.reference_app.server import ReferenceApp, create_app
from examples.reference_app.services import AuditTrail, WorkspaceRepository
from falcon_pachinko import WebSocketConnectionManager
from falcon_pachinko.testing import WebSocketTestClient, serve_asgi_app

_TASKS_PATH = "/ws/workspaces/atlas/projects/triage/tasks"
_WAIT_TIMEOUT = 3.0
_AUDIT_EVENTS = {
    "workspace.loaded",
    "project.loaded",
    "session.open",
    "message.received",
    "message.processed",
    "session.closed",
}

if typ.TYPE_CHECKING:
    import collections.abc as cabc


class _LiveSession(typ.Protocol):
    """The public client operations used by a live scenario."""

    async def send_json(self, payload: object) -> None: ...

    async def receive_json(self) -> object: ...

    async def close(self, code: int = 1000, reason: str = "") -> None: ...


@dc.dataclass(slots=True)
class LiveReferenceContext:
    """Shared state for one live reference application scenario."""

    event_loop: asyncio.AbstractEventLoop
    stack: contextlib.AsyncExitStack
    app: ReferenceApp
    client: WebSocketTestClient
    session: _LiveSession | None = None
    connection_id: str | None = None
    handshake_status: int | None = None
    ready_message: object | None = None
    task_reply: object | None = None
    announcement: object | None = None


@pytest.fixture
def event_loop(
    event_loop_policy: asyncio.AbstractEventLoopPolicy,
) -> cabc.Iterator[asyncio.AbstractEventLoop]:
    """Provide a dedicated event loop for each BDD scenario."""
    loop = event_loop_policy.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        yield loop
    finally:
        asyncio.set_event_loop(None)
        loop.close()


@pytest.fixture
def async_exit_stack(
    event_loop: asyncio.AbstractEventLoop,
) -> cabc.Iterator[contextlib.AsyncExitStack]:
    """Close server and client resources on the scenario's event loop."""
    stack = contextlib.AsyncExitStack()
    try:
        yield stack
    finally:
        event_loop.run_until_complete(stack.aclose())


@scenario(
    "reference_example_live.feature",
    "authenticated task creation and workspace announcement",
)
def test_live_reference_task_flow() -> None:  # pragma: no cover - BDD registration
    """Register the successful live reference application journey."""


@scenario(
    "reference_example_live.feature",
    "invalid workspace token is rejected during the handshake",
)
def test_live_reference_rejects_invalid_token() -> (
    None
):  # pragma: no cover - BDD registration
    """Register the live authentication rejection journey."""


@given(
    "the reference application is running on a live Falcon ASGI server",
    target_fixture="context",
)
def given_live_reference_app(
    event_loop: asyncio.AbstractEventLoop,
    async_exit_stack: contextlib.AsyncExitStack,
) -> LiveReferenceContext:
    """Start the production factory and client on one managed async stack."""
    app = create_app()
    base_url = event_loop.run_until_complete(
        async_exit_stack.enter_async_context(serve_asgi_app(app))
    )
    client = WebSocketTestClient(base_url, allow_insecure=True)
    return LiveReferenceContext(
        event_loop=event_loop,
        stack=async_exit_stack,
        app=app,
        client=client,
    )


@when(
    'a client connects to "/ws/workspaces/atlas/projects/triage/tasks" '
    'with workspace token "seekrit" and user "casey"',
    target_fixture="context",
)
def when_valid_client_connects(context: LiveReferenceContext) -> LiveReferenceContext:
    """Open the documented nested route with its documented auth headers."""

    async def connect() -> _LiveSession:
        session = await context.stack.enter_async_context(
            context.client.connect(
                _TASKS_PATH,
                headers={"x-workspace-token": "seekrit", "x-user": "casey"},
            )
        )
        return typ.cast("_LiveSession", session)

    context.session = context.event_loop.run_until_complete(connect())
    return context


@then('the client receives session.ready for workspace "atlas" and project "triage"')
def then_session_ready(context: LiveReferenceContext) -> None:
    """Read the first server event from the real client connection."""
    session = _require_session(context)
    context.ready_message = context.event_loop.run_until_complete(
        asyncio.wait_for(session.receive_json(), timeout=_WAIT_TIMEOUT)
    )
    message = _mapping(context.ready_message, "session.ready must be a mapping")
    assert message.get("type") == "session.ready", (
        "the accepted route must send session.ready first"
    )
    payload = _mapping(
        message.get("payload"), "session.ready payload must be a mapping"
    )
    assert payload.get("workspace") == "atlas", "session.ready must name atlas"
    assert payload.get("project") == "triage", "session.ready must name triage"


@then('the connection manager registers the client in workspace "atlas"')
def then_connection_registered(context: LiveReferenceContext) -> None:
    """Verify that the accepted socket is registered and joined to its room."""
    manager = _connection_manager(context)
    sockets = manager.websockets
    assert len(sockets) == 1, "one live client must be registered"
    context.connection_id = next(iter(sockets))
    room_members = manager.rooms.get("workspace:atlas", frozenset())
    assert context.connection_id in room_members, (
        "the accepted connection must join workspace:atlas"
    )


@when('the client sends task.add for task "T-42"', target_fixture="context")
def when_client_adds_task(context: LiveReferenceContext) -> LiveReferenceContext:
    """Send a real tagged JSON message through Falcon's responder loop."""
    session = _require_session(context)
    context.event_loop.run_until_complete(
        session.send_json({
            "type": "task.add",
            "task_id": "T-42",
            "title": "Investigate live WebSocket handling",
        })
    )
    return context


@then('the client receives task.added for task "T-42"')
def then_task_added(context: LiveReferenceContext) -> None:
    """Check the schema-decoded handler response sent over the live socket."""
    session = _require_session(context)
    context.task_reply = context.event_loop.run_until_complete(
        asyncio.wait_for(session.receive_json(), timeout=_WAIT_TIMEOUT)
    )
    message = _mapping(context.task_reply, "task.added must be a mapping")
    assert message.get("type") == "task.added", "task.add must receive task.added"
    payload = _mapping(message.get("payload"), "task.added payload must be a mapping")
    assert payload.get("task_id") == "T-42", "the acknowledgement must name T-42"
    assert payload.get("project_id") == "triage", (
        "the acknowledgement must retain the nested project"
    )


@then("the worker broadcasts task_added to the client")
def then_worker_broadcast(context: LiveReferenceContext) -> None:
    """Receive the worker's room broadcast through the same client socket."""
    session = _require_session(context)
    context.announcement = context.event_loop.run_until_complete(
        asyncio.wait_for(session.receive_json(), timeout=_WAIT_TIMEOUT)
    )
    message = _mapping(context.announcement, "the announcement must be a mapping")
    assert message.get("type") == "announcement", (
        "the lifespan worker must broadcast an announcement"
    )
    payload = _mapping(message.get("payload"), "announcement payload must be a mapping")
    assert payload.get("kind") == "task_added", (
        "the worker announcement must identify task_added"
    )
    assert payload.get("task_id") == "T-42", (
        "the worker announcement must identify the new task"
    )


@when("the client disconnects with code 1000", target_fixture="context")
def when_client_disconnects(context: LiveReferenceContext) -> LiveReferenceContext:
    """Close the client normally and let Falcon complete responder cleanup."""
    session = _require_session(context)
    context.event_loop.run_until_complete(session.close(code=1000))
    return context


@then("the connection manager removes the client")
def then_connection_removed(context: LiveReferenceContext) -> None:
    """Poll until the live adapter has finalized connection cleanup."""
    connection_id = context.connection_id
    assert connection_id is not None, "the accepted connection ID must be known"
    manager = _connection_manager(context)

    async def wait_for_removal() -> None:
        deadline = context.event_loop.time() + _WAIT_TIMEOUT
        while connection_id in manager.websockets:
            if context.event_loop.time() >= deadline:
                raise TimeoutError
            await asyncio.sleep(0.01)

    context.event_loop.run_until_complete(
        asyncio.wait_for(wait_for_removal(), timeout=_WAIT_TIMEOUT)
    )
    assert connection_id not in manager.websockets, (
        "normal disconnect must remove the socket from the manager"
    )
    assert "workspace:atlas" not in manager.rooms, (
        "removing the final connection must clear its workspace room"
    )


@then("the reference hooks and services record this connection's full journey")
def then_audit_journey(context: LiveReferenceContext) -> None:
    """Check hook audit records and injected repository state for one ID."""
    connection_id = context.connection_id
    assert connection_id is not None, "the accepted connection ID must be known"
    audit = _audit_trail(context)
    records = [
        record for record in audit.records if record.get("event") in _AUDIT_EVENTS
    ]
    events = {record.get("event") for record in records}
    assert events == _AUDIT_EVENTS, (
        "all connect, receive, process, and disconnect hook events must be recorded"
    )
    metadata = [
        _mapping(record.get("metadata"), "audit metadata must be a mapping")
        for record in records
    ]
    assert all(item.get("connection") == connection_id for item in metadata), (
        "hooks across the full live journey must share its connection ID"
    )
    closed = next(
        item
        for record, item in zip(records, metadata, strict=True)
        if record.get("event") == "session.closed"
    )
    assert closed.get("close_code") == 1000, "the audit must retain normal close code"

    repository = context.app.reference_services.resolve("workspace_repo")
    assert isinstance(repository, WorkspaceRepository), (
        "the application container must expose the injected workspace repository"
    )
    tasks = context.event_loop.run_until_complete(
        repository.list_tasks("atlas", "triage")
    )
    assert any(
        task.task_id == "T-42" and task.created_by == "casey" for task in tasks
    ), (
        "the live handler must persist T-42 and its authenticated author "
        "through the injected repository"
    )


@when(
    'a client connects to "/ws/workspaces/atlas/projects/triage/tasks" '
    'with workspace token "wrong" and user "casey"',
    target_fixture="context",
)
def when_invalid_client_connects(context: LiveReferenceContext) -> LiveReferenceContext:
    """Attempt the real handshake and retain Falcon's HTTP rejection status."""

    async def connect() -> None:
        await context.stack.enter_async_context(
            context.client.connect(
                _TASKS_PATH,
                headers={"x-workspace-token": "wrong", "x-user": "casey"},
            )
        )

    with pytest.raises(InvalidStatusCode) as caught:
        context.event_loop.run_until_complete(connect())
    context.handshake_status = caught.value.status_code
    return context


@then("the WebSocket handshake fails with status 403")
def then_handshake_rejected(context: LiveReferenceContext) -> None:
    """Assert that the server rejects the HTTP upgrade for invalid auth."""
    assert context.handshake_status == 403, (
        "invalid workspace credentials must fail the WebSocket handshake"
    )


@then("the reference audit trail records invalid_token")
def then_invalid_token_audited(context: LiveReferenceContext) -> None:
    """Confirm the auth hook records a bounded rejection reason."""
    rejections = [
        record
        for record in _audit_trail(context).records
        if record.get("event") == "auth.rejected"
    ]
    assert len(rejections) == 1, "the failed handshake must produce one audit record"
    metadata = _mapping(rejections[0].get("metadata"), "rejection metadata must map")
    assert metadata.get("reason") == "invalid_token", (
        "the auth hook must distinguish an invalid token"
    )
    assert "token" not in metadata, "the rejected credential must not be audited"


def _require_session(context: LiveReferenceContext) -> _LiveSession:
    """Return the active WebSocket session or fail with a useful message."""
    session = context.session
    assert session is not None, "the scenario must establish a WebSocket session"
    return session


def _connection_manager(context: LiveReferenceContext) -> WebSocketConnectionManager:
    """Resolve the connection manager exposed by the production app container."""
    manager = context.app.reference_services.resolve("conn_mgr")
    assert isinstance(manager, WebSocketConnectionManager), (
        "the reference container must expose its connection manager"
    )
    return manager


def _audit_trail(context: LiveReferenceContext) -> AuditTrail:
    """Resolve the audit service exposed by the production app container."""
    audit = context.app.reference_services.resolve("audit_trail")
    assert isinstance(audit, AuditTrail), (
        "the reference container must expose its audit trail"
    )
    return audit


def _mapping(value: object, message: str) -> cabc.Mapping[str, object]:
    """Validate a mapping from a received client frame or audit entry."""
    assert isinstance(value, dict), message
    return value
