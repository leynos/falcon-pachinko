"""Unit tests for accepted reference-resource setup and rollback."""

from __future__ import annotations

import asyncio
import contextlib
import typing as typ

import pytest

from examples.reference_app.resources import TaskStreamResource
from falcon_pachinko.router import WebSocketRouter
from falcon_pachinko.websocket import InProcessBackend, WebSocketConnectionManager
from tests._reference_example_support import (
    _PROJECT,
    _WORKSPACE,
    _build_resource,
    _connect_request,
)
from tests._stubs import RecordingWebSocket

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from examples.reference_app.services import AuditTrail


@pytest.mark.asyncio
@pytest.mark.parametrize("receive_loop", [False, True])
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
            1001,
            id="accept-cancellation",
        ),
        pytest.param(
            "ready",
            asyncio.CancelledError("ready send cancelled"),
            1001,
            id="ready-cancellation",
        ),
    ],
)
async def test_accepted_setup_failure_is_removed_and_audited_by_router(
    stage: str, failure: BaseException, expected_code: int, *, receive_loop: bool
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
        receive_loop=receive_loop,
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
    assert resource._conn_id is None, "cleanup must clear the resource identity"
    closed = [record for record in audit.records if record["event"] == "session.closed"]
    assert len(closed) == 1, "accepted setup failure must be audited exactly once"
    metadata = typ.cast("dict[str, object]", closed[0]["metadata"])
    assert metadata["close_code"] == expected_code, (
        "closure must retain the session policy code"
    )


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
async def test_accepted_setup_cleanup_joins_through_repeated_cancellation(
    *, setup_fails: bool
) -> None:
    """Do not leave accepted membership when cancellation interrupts cleanup."""
    scenario = _build_accepted_cleanup_scenario(setup_fails=setup_fails)
    with _capture_loop_exception_context() as reported:
        task = asyncio.create_task(
            scenario.router.on_websocket(_connect_request(), scenario.socket)
        )
        try:
            await asyncio.wait_for(scenario.backend.joining.wait(), timeout=1)
            if not setup_fails:
                task.cancel("setup cancelled")
            await asyncio.wait_for(scenario.backend.removing.wait(), timeout=1)
            for _ in range(2):
                task.cancel("rollback interrupted")
                await asyncio.sleep(0)
            assert not task.done(), "the session must join rollback before exiting"
            scenario.backend.release_removal.set()
            message = "rollback interrupted" if setup_fails else "setup cancelled"
            with pytest.raises(asyncio.CancelledError, match=message):
                await asyncio.wait_for(task, timeout=1)
            assert not scenario.manager.websockets, (
                "accepted cleanup must remove membership"
            )
            assert not scenario.socket.unaccepted, (
                "the router must observe resource acceptance"
            )
            _assert_one_session_closed(
                scenario.audit, close_code=1011 if setup_fails else 1001
            )
            if setup_fails:
                assert any(
                    context.get("exception") is scenario.backend.setup_error
                    for context in reported
                ), "bare cancellation must report the superseded setup failure"
            else:
                assert not reported, (
                    "cancellation alone must not report a session failure"
                )
        finally:
            scenario.backend.release_removal.set()
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)


def _assert_one_session_closed(audit: AuditTrail, *, close_code: int) -> None:
    """Assert that accepted cleanup recorded its single effective code."""
    closed = [record for record in audit.records if record["event"] == "session.closed"]
    assert len(closed) == 1, "accepted setup failure must be audited once"
    metadata = typ.cast("dict[str, object]", closed[0]["metadata"])
    assert metadata["close_code"] == close_code, (
        "accepted cleanup must preserve the failure or cancellation code"
    )


@pytest.mark.asyncio
async def test_direct_setup_rollback_preserves_cancellation_if_removal_fails() -> None:
    """Cancellation remains primary when the pending removal also fails."""
    backend = FailingPendingRollbackBackend()
    manager = WebSocketConnectionManager(backend=backend)
    resource, _repo, _audit, _feed = _build_resource(manager)

    async def capture_connect_cancellation() -> asyncio.CancelledError:
        """Retain cancellation metadata before ``wait_for`` observes it."""
        try:
            await resource.on_connect(
                _connect_request(),
                RecordingWebSocket(),
                workspace_id=_WORKSPACE,
                project_id=_PROJECT,
            )
        except asyncio.CancelledError as error:
            return error
        raise AssertionError

    task = asyncio.create_task(capture_connect_cancellation())
    try:
        await asyncio.wait_for(backend.removing.wait(), timeout=1)
        task.cancel("rollback interrupted")
        backend.release_removal.set()

        raised = await asyncio.wait_for(task, timeout=1)
        assert str(raised) == "rollback interrupted", (
            "the wrapper must preserve the cancellation message"
        )

        assert raised.__cause__ is backend.removal_error, (
            "the failed removal must remain chained to cancellation"
        )
        assert raised.__context__ is backend.setup_error, (
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


class _AcceptedCleanupScenario(typ.NamedTuple):
    """Group state needed to exercise accepted-session cleanup."""

    backend: BlockingRollbackBackend
    manager: WebSocketConnectionManager
    audit: AuditTrail
    socket: RecordingWebSocket
    router: WebSocketRouter


def _build_accepted_cleanup_scenario(
    *,
    setup_fails: bool,
) -> _AcceptedCleanupScenario:
    """Build an accepted session whose room setup blocks or fails."""
    backend = BlockingRollbackBackend(setup_fails=setup_fails)
    manager = WebSocketConnectionManager(backend=backend)
    resource, _, audit, _ = _build_resource(manager)
    socket = RecordingWebSocket()
    router = WebSocketRouter(resource_factory=lambda _factory: resource)
    router.add_route(
        "/workspaces/{workspace_id}/projects/{project_id}/tasks", TaskStreamResource
    )
    router.mount("/ws")
    return _AcceptedCleanupScenario(backend, manager, audit, socket, router)


@contextlib.contextmanager
def _capture_loop_exception_context() -> cabc.Iterator[list[dict[str, object]]]:
    """Capture loop-reported failures until the context exits."""
    loop = asyncio.get_running_loop()
    previous_handler = loop.get_exception_handler()
    reported: list[dict[str, object]] = []
    loop.set_exception_handler(lambda _loop, context: reported.append(context))
    try:
        yield reported
    finally:
        loop.set_exception_handler(previous_handler)


@pytest.mark.asyncio
async def test_on_connect_cleans_up_and_reraises_when_join_room_fails() -> None:
    """An accepted room-join failure rolls back direct on_connect membership."""
    conn_mgr = WebSocketConnectionManager(backend=FailingJoinBackend())
    resource, _repo, _audit, _feed = _build_resource(conn_mgr)
    ws = RecordingWebSocket()

    with pytest.raises(RuntimeError, match="cannot join"):
        await resource.on_connect(
            _connect_request(), ws, workspace_id=_WORKSPACE, project_id=_PROJECT
        )

    remaining = conn_mgr.websockets
    assert remaining == {}, f"the failed connection must be removed: {remaining}"
    assert resource._conn_id is None, "direct rollback must clear the identity"
    assert not ws.unaccepted, "room setup follows websocket acceptance"
    assert not ws.messages, "no session.ready message is sent on failure"


@pytest.mark.asyncio
async def test_on_connect_accepts_before_joining_workspace_room() -> None:
    """Room broadcasts cannot select a socket while its handshake is pending."""
    manager = WebSocketConnectionManager()
    resource, _repo, _audit, _feed = _build_resource(manager)
    accept_started = asyncio.Event()
    finish_accept = asyncio.Event()

    class PausingSocket(RecordingWebSocket):
        async def accept(self, subprotocol: str | None = None) -> None:
            accept_started.set()
            await finish_accept.wait()
            await super().accept(subprotocol)

        async def send_media(self, data: object) -> None:
            if self.unaccepted:
                msg = "cannot send to an unaccepted websocket"
                raise RuntimeError(msg)
            await super().send_media(data)

    socket = PausingSocket()
    task = asyncio.create_task(
        resource.on_connect(
            _connect_request(),
            socket,
            workspace_id=_WORKSPACE,
            project_id=_PROJECT,
        )
    )
    try:
        await asyncio.wait_for(accept_started.wait(), timeout=1)
        await asyncio.wait_for(
            manager.broadcast_to_room(
                f"workspace:{_WORKSPACE}", {"type": "announcement"}
            ),
            timeout=1,
        )
        assert not socket.messages, "an unaccepted socket must not receive a broadcast"
    finally:
        finish_accept.set()
        await asyncio.wait_for(task, timeout=1)


@pytest.mark.asyncio
async def test_on_connect_groups_setup_and_rollback_failures_primary_first() -> None:
    """Keep the setup error primary when direct rollback also fails."""
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
