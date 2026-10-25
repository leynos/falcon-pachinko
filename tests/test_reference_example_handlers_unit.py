"""Behavioural unit tests for ``TaskStreamResource`` handlers and lifecycle."""

from __future__ import annotations

import asyncio
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
from falcon_pachinko.router import WebSocketRouter
from falcon_pachinko.websocket import InProcessBackend, WebSocketConnectionManager
from tests._stubs import RecordingWebSocket, RequestStub

if typ.TYPE_CHECKING:
    import falcon

    from falcon_pachinko.protocols import WebSocketLike

_WORKSPACE = "atlas"
_PROJECT = "triage"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("stage", "failure", "expected_code"),
    [
        pytest.param(
            "accept", RuntimeError("accepted setup failed"), 1011, id="accept-failure"
        ),
        pytest.param(
            "ready", RuntimeError("ready send failed"), 1011, id="ready-failure"
        ),
        pytest.param(
            "accept",
            asyncio.CancelledError("accepted setup cancelled"),
            1006,
            id="accept-cancellation",
        ),
        pytest.param(
            "ready",
            asyncio.CancelledError("ready send cancelled"),
            1006,
            id="ready-cancellation",
        ),
    ],
)
async def test_accepted_setup_failure_is_removed_and_audited_by_router(
    stage: str, failure: BaseException, expected_code: int
) -> None:
    """Preserve accepted session identity until the framework runs cleanup."""
    manager = WebSocketConnectionManager()
    resource, _repo, audit, _feed = _build_resource(manager)

    class FailingSetupSocket(RecordingWebSocket):
        async def accept(self, subprotocol: str | None = None) -> None:
            """Fail after recording transport acceptance when requested."""
            await super().accept(subprotocol)
            if stage == "accept":
                raise failure

        async def send_media(self, data: object) -> None:
            """Fail the readiness send after the handshake completes."""
            if stage == "ready":
                raise failure
            await super().send_media(data)

    socket = FailingSetupSocket()
    router = WebSocketRouter(
        resource_factory=lambda _factory: resource,
        receive_loop=True,
    )
    router.add_route(
        "/workspaces/{workspace_id}/projects/{project_id}/tasks", TaskStreamResource
    )
    router.mount("/ws")
    with pytest.raises(type(failure)) as raised:
        await asyncio.wait_for(
            router.on_websocket(_connect_request(), socket), timeout=1
        )
    assert raised.value is failure, "cleanup must preserve the original setup failure"
    assert not manager.websockets, "accepted setup failures must release membership"
    closed = [record for record in audit.records if record["event"] == "session.closed"]
    assert len(closed) == 1, "accepted setup failure must be audited exactly once"
    metadata = typ.cast("dict[str, object]", closed[0]["metadata"])
    assert metadata["close_code"] == expected_code, (
        "closure must retain the session policy code"
    )


@pytest.mark.asyncio
async def test_accepted_ready_failure_is_removed_and_audited_by_router() -> None:
    """An accepted setup failure reaches router-owned disconnect cleanup."""
    manager = WebSocketConnectionManager()
    resource, _repo, audit, _feed = _build_resource(manager)
    failure = RuntimeError("ready send failed")

    class FailingReadySocket(RecordingWebSocket):
        async def send_media(self, data: object) -> None:
            """Fail readiness after accepting under the default router mode."""
            del data
            raise failure

    router = WebSocketRouter(resource_factory=lambda _factory: resource)
    router.add_route(
        "/workspaces/{workspace_id}/projects/{project_id}/tasks", TaskStreamResource
    )
    router.mount("/ws")
    with pytest.raises(RuntimeError, match="ready send failed") as raised:
        await asyncio.wait_for(
            router.on_websocket(_connect_request(), FailingReadySocket()), timeout=1
        )

    assert raised.value is failure, "rollback must preserve the original failure"
    assert manager.websockets == {}, "failed setup must not retain manager membership"
    assert resource._conn_id is None, "rollback must clear the resource identity"
    closed = [record for record in audit.records if record["event"] == "session.closed"]
    assert len(closed) == 1, "accepted setup failure must be audited exactly once"
    metadata = typ.cast("dict[str, object]", closed[0]["metadata"])
    assert metadata["close_code"] == 1011, "setup failure must use code 1011"


class FailingJoinBackend(InProcessBackend):
    """In-process backend whose ``join_room`` always fails.

    Everything else is the real backend, so the connection the resource adds
    before joining is genuinely registered and genuinely removed on cleanup.
    """

    async def join_room(self, conn_id: str, room: str) -> None:
        """Raise, simulating a broken room-membership backend."""
        msg = f"cannot join {room!r}"
        raise RuntimeError(msg)


class FailingRollbackBackend(FailingJoinBackend):
    """Fail setup and removal so rollback precedence stays observable."""

    def __init__(self) -> None:
        """Keep both failures available for identity assertions."""
        super().__init__()
        self.setup_error = RuntimeError("cannot join workspace")
        self.removal_error = RuntimeError("cannot remove connection")

    async def join_room(self, conn_id: str, room: str) -> None:
        """Raise the setup failure after the socket has been registered."""
        del conn_id, room
        raise self.setup_error

    async def remove_connection(self, conn_id: str) -> None:
        """Leave registered membership in place while surfacing rollback failure."""
        del conn_id
        raise self.removal_error


class BlockingRollbackBackend(InProcessBackend):
    """Pause setup and rollback separately to expose repeated cancellation."""

    def __init__(self, *, setup_fails: bool) -> None:
        """Expose setup and rollback intervals independently."""
        super().__init__()
        self.joining = asyncio.Event()
        self.removing = asyncio.Event()
        self.release_removal = asyncio.Event()
        self.setup_error = RuntimeError("setup failed")
        self.setup_fails = setup_fails

    async def join_room(self, conn_id: str, room: str) -> None:
        """Keep setup pending until its owning session is cancelled."""
        self.joining.set()
        if self.setup_fails:
            raise self.setup_error
        await asyncio.Event().wait()

    async def remove_connection(self, conn_id: str) -> None:
        """Expose the rollback interval before removing real membership."""
        self.removing.set()
        await self.release_removal.wait()
        await super().remove_connection(conn_id)


class FailingPendingRollbackBackend(BlockingRollbackBackend):
    """Fail removal after the setup task is cancelled while joining it."""

    def __init__(self) -> None:
        """Retain the setup and rollback failures for precedence assertions."""
        super().__init__(setup_fails=True)
        self.removal_error = RuntimeError("removal failed after cancellation")

    async def remove_connection(self, conn_id: str) -> None:
        """Wait for the test to expose cancellation, then fail removal."""
        del conn_id
        self.removing.set()
        await self.release_removal.wait()
        raise self.removal_error


@pytest.mark.asyncio
@pytest.mark.parametrize("setup_fails", [False, True], ids=["cancelled", "failed"])
async def test_pre_accept_rollback_joins_through_repeated_cancellation(
    *, setup_fails: bool
) -> None:
    """Do not leave membership behind when cancellation interrupts rollback."""
    backend = BlockingRollbackBackend(setup_fails=setup_fails)
    manager = WebSocketConnectionManager(backend=backend)
    resource, _repo, audit, _feed = _build_resource(manager)
    socket = RecordingWebSocket()
    router = WebSocketRouter(resource_factory=lambda _factory: resource)
    router.add_route(
        "/workspaces/{workspace_id}/projects/{project_id}/tasks", TaskStreamResource
    )
    router.mount("/ws")
    task = asyncio.create_task(router.on_websocket(_connect_request(), socket))
    try:
        await asyncio.wait_for(backend.joining.wait(), timeout=1)
        if not setup_fails:
            task.cancel("setup cancelled")
        await asyncio.wait_for(backend.removing.wait(), timeout=1)
        for _ in range(2):
            task.cancel("rollback interrupted")
            await asyncio.sleep(0)
        assert not task.done(), "the session must join rollback before exiting"
        backend.release_removal.set()
        expected_message = "rollback interrupted" if setup_fails else "setup cancelled"
        with pytest.raises(asyncio.CancelledError, match=expected_message) as raised:
            await asyncio.wait_for(task, timeout=1)
        if setup_fails:
            assert raised.value.__cause__ is backend.setup_error, (
                "cancellation must retain the original setup failure as its cause"
            )
        assert not manager.websockets, "pre-accept rollback must remove membership"
        assert socket.unaccepted, "this path must not establish a session"
        assert not audit.records, "pre-accept cancellation must not audit closure"
    finally:
        backend.release_removal.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_pre_accept_rollback_preserves_cancellation_if_removal_fails() -> None:
    """Cancellation remains primary when the pending removal also fails."""
    backend = FailingPendingRollbackBackend()
    manager = WebSocketConnectionManager(backend=backend)
    resource, _repo, _audit, _feed = _build_resource(manager)
    task = asyncio.create_task(
        resource.on_connect(
            _connect_request(),
            RecordingWebSocket(),
            workspace_id=_WORKSPACE,
            project_id=_PROJECT,
        )
    )
    try:
        await asyncio.wait_for(backend.removing.wait(), timeout=1)
        task.cancel("rollback interrupted")
        backend.release_removal.set()

        with pytest.raises(
            asyncio.CancelledError, match="rollback interrupted"
        ) as raised:
            await asyncio.wait_for(task, timeout=1)

        assert raised.value.__cause__ is backend.removal_error, (
            "the failed removal must remain chained to cancellation"
        )
        assert raised.value.__context__ is backend.setup_error, (
            "the original setup failure must remain in the exception context"
        )
        assert resource._conn_id is not None, (
            "failed removal must retain the resource's membership identity"
        )
    finally:
        backend.release_removal.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        if resource._conn_id is not None:
            await InProcessBackend.remove_connection(backend, resource._conn_id)
            resource._conn_id = None


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
    """A pre-accept room-join failure rolls back on_connect membership."""
    conn_mgr = WebSocketConnectionManager(backend=FailingJoinBackend())
    resource, _repo, _audit, _feed = _build_resource(conn_mgr)
    ws = RecordingWebSocket()

    with pytest.raises(RuntimeError, match="cannot join"):
        await resource.on_connect(
            _connect_request(), ws, workspace_id=_WORKSPACE, project_id=_PROJECT
        )

    remaining = conn_mgr.websockets
    assert remaining == {}, f"the failed connection must be removed: {remaining}"
    assert resource._conn_id is None, "pre-accept rollback must clear the identity"
    assert ws.unaccepted, "setup failure must happen before websocket acceptance"
    assert not ws.messages, "no session.ready message is sent on failure"


@pytest.mark.asyncio
async def test_on_connect_groups_setup_and_rollback_failures_primary_first() -> None:
    """Keep the setup error primary when pre-accept removal also fails."""
    backend = FailingRollbackBackend()
    conn_mgr = WebSocketConnectionManager(backend=backend)
    resource, _repo, _audit, _feed = _build_resource(conn_mgr)

    with pytest.raises(ExceptionGroup) as raised:
        await resource.on_connect(
            _connect_request(),
            RecordingWebSocket(),
            workspace_id=_WORKSPACE,
            project_id=_PROJECT,
        )

    assert raised.value.exceptions == (backend.setup_error, backend.removal_error), (
        "rollback failure must be grouped after the original setup failure"
    )
    assert resource._conn_id is not None, "failed removal must retain its identity"
    await InProcessBackend.remove_connection(backend, resource._conn_id)


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
    resource, _, audit, _ = _build_resource(conn_mgr)
    ws = RecordingWebSocket()
    await resource.on_connect(
        _connect_request(), ws, workspace_id=_WORKSPACE, project_id=_PROJECT
    )
    conn_id = resource._conn_id
    assert conn_id is not None, "on_connect must assign a connection ID"
    assert conn_mgr.websockets == {conn_id: ws}, (
        "on_connect must register the effective socket"
    )
    assert not ws.unaccepted, "on_connect must accept before readiness"
    assert ws.messages == [
        {
            "type": "session.ready",
            "payload": {"workspace": _WORKSPACE, "project": _PROJECT},
        }
    ], "on_connect must send the session-ready event after acceptance"
    opened = [record for record in audit.records if record["event"] == "session.open"]
    assert len(opened) == 1, "on_connect must record exactly one session.open event"
    opened_metadata = opened[0]["metadata"]
    assert isinstance(opened_metadata, dict), "open metadata must be a mapping"
    assert opened_metadata["connection"] == conn_id, "open must carry this connection"

    await resource.on_disconnect(ws, close_code=1001)
    await resource.on_disconnect(ws, close_code=1001)

    assert conn_mgr.websockets == {}, "the connection must be removed on disconnect"
    assert resource._conn_id is None, "disconnect must clear the resource identity"
    closed = [r for r in audit.records if r["event"] == "session.closed"]
    assert len(closed) == 1, f"exactly one session.closed record is expected: {closed}"
    closed_metadata = closed[0]["metadata"]
    assert isinstance(closed_metadata, dict), "audit metadata must be a mapping"
    assert closed_metadata["connection"] == conn_id, (
        "close must carry the same connection"
    )
    assert closed_metadata["close_code"] == 1001, "close_code must match"
    assert closed_metadata["workspace"] == _WORKSPACE, "workspace must match"
    assert closed_metadata["project"] == _PROJECT, "project must match"


class EnvelopeTaskStreamResource(TaskStreamResource):
    """``TaskStreamResource`` without a schema, so envelopes are decoded.

    The reference deployment compiles its inbound payloads before they reach
    the WebSocket loop. A deployment that skips that compilation still hands
    the same class to ``dispatch()``, and the envelope path is then peer
    steerable. This subclass keeps the real lifecycle and collaborators while
    forcing that path.
    """

    schema = None


def _envelope_resource(
    conn_mgr: WebSocketConnectionManager,
) -> EnvelopeTaskStreamResource:
    """Build an envelope-dispatching task stream sharing real collaborators."""
    resource, _repo, _audit, _feed = _build_resource(conn_mgr)
    return EnvelopeTaskStreamResource(
        workspace_repo=resource._repo,
        audit_trail=resource._audit,
        announcement_feed=resource._feed,
        conn_mgr=conn_mgr,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("tag", ["disconnect", "Disconnect", "DISCONNECT"])
async def test_reserved_envelope_tag_leaves_live_membership_intact(tag: str) -> None:
    """A ``disconnect`` envelope cannot evict a live connection.

    The payload is a valid close code, so nothing but the reserved-name
    refusal stops ``on_disconnect`` from tearing down a connected session.
    """
    conn_mgr = WebSocketConnectionManager()
    resource = _envelope_resource(conn_mgr)
    ws = RecordingWebSocket()
    await resource.on_connect(
        _connect_request(), ws, workspace_id=_WORKSPACE, project_id=_PROJECT
    )
    await resource.on_accepted(ws)

    await _dispatch(resource, ws, {"type": tag, "payload": 1000})

    assert dict(conn_mgr.websockets) == {resource._conn_id: ws}, (
        f"the live connection must survive a reserved envelope: {conn_mgr.websockets}"
    )
    assert dict(conn_mgr.rooms) == {f"workspace:{_WORKSPACE}": {resource._conn_id}}, (
        f"workspace membership must be unchanged: {conn_mgr.rooms}"
    )
    assert not [r for r in resource._audit.records if r["event"] == "session.closed"], (
        "a reserved envelope must not audit a closure"
    )
    assert ws.messages[-1] == {
        "type": "error",
        "payload": "unsupported message",
    }, f"the application fallback must answer the reserved envelope: {ws.messages}"


@pytest.mark.asyncio
async def test_reserved_envelope_cannot_evict_through_real_lifecycle() -> None:
    """A ``disconnect`` envelope cannot reach the real cleanup callback.

    The close code is type-correct for ``on_disconnect``, so only the
    reserved-name refusal stands between the frame and a live-connection
    teardown.
    """
    conn_mgr = WebSocketConnectionManager()
    resource = _envelope_resource(conn_mgr)
    ws = RecordingWebSocket()
    await resource.on_connect(
        _connect_request(), ws, workspace_id=_WORKSPACE, project_id=_PROJECT
    )
    await resource.on_accepted(ws)
    conn_id = resource._conn_id

    await _dispatch(resource, ws, {"type": "disconnect", "payload": 1000})

    assert dict(conn_mgr.websockets) == {conn_id: ws}, (
        f"the live connection must survive: {conn_mgr.websockets}"
    )
    assert dict(conn_mgr.rooms) == {f"workspace:{_WORKSPACE}": {conn_id}}, (
        f"workspace membership must be unchanged: {conn_mgr.rooms}"
    )
    assert not [r for r in resource._audit.records if r["event"] == "session.closed"], (
        "the reserved envelope must not audit a closure"
    )
    assert ws.messages[-1] == {
        "type": "error",
        "payload": "unsupported message",
    }, f"the application fallback must answer the frame: {ws.messages}"

    # The session must still be usable afterwards, not merely reported live.
    await _dispatch(resource, ws, {"type": "task.list", "payload": {}})
    last = ws.messages[-1]
    assert isinstance(last, dict), f"the reply must be a mapping: {ws.messages}"
    assert last["type"] == "task.list", (
        f"a genuine message must still be served: {ws.messages}"
    )


@pytest.mark.asyncio
async def test_connect_envelope_tag_does_not_reconnect() -> None:
    """A ``connect`` envelope cannot re-run the connect lifecycle."""
    conn_mgr = WebSocketConnectionManager()
    resource = _envelope_resource(conn_mgr)
    ws = RecordingWebSocket()
    await resource.on_connect(
        _connect_request(), ws, workspace_id=_WORKSPACE, project_id=_PROJECT
    )
    await resource.on_accepted(ws)
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
