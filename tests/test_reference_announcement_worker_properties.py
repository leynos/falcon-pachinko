"""Property tests for the reference announcement worker's recovery invariants."""

from __future__ import annotations

import asyncio
import logging
import typing as typ

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from examples.reference_app.resources import _workspace_room
from examples.reference_app.workers import announcement_worker
from falcon_pachinko.websocket import WebSocketConnectionManager
from falcon_pachinko.workers import WorkerController
from tests._stubs import (
    BlockingWebSocket,
    DisconnectingWebSocket,
    FailingOnceWebSocket,
    RecordingWebSocket,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from examples.reference_app.services import AnnouncementFeed

WORKSPACES = ("atlas", "zephyr")
FAULT_KINDS = ("disconnect", "timeout", "unexpected")
SEND_TIMEOUT = 0.01
RESTART_DELAY = 0.0
EVENT_WAIT_TIMEOUT = 5.0
WORKER_LOGGER = "examples.reference_app.workers"


class _ScriptedFeed:
    """Feed double that signals once every scripted announcement is consumed."""

    def __init__(self, events: list[tuple[str, dict[str, object]]]) -> None:
        """Keep the scripted events and create the consumed signal."""
        self._events = list(events)
        self.consumed = asyncio.Event()

    async def next_event(self) -> tuple[str, dict[str, object]]:
        """Return the next event, then block until the worker is cancelled."""
        if self._events:
            return self._events.pop(0)
        self.consumed.set()
        await asyncio.Event().wait()
        msg = "the scripted feed is cancelled once it has been drained"
        raise AssertionError(msg)  # pragma: no cover - unreachable by design


class _ListHandler(logging.Handler):
    """Collect log records so a property example can assert on them."""

    def __init__(self, records: list[logging.LogRecord]) -> None:
        """Keep the list that receives every emitted record."""
        super().__init__(level=logging.DEBUG)
        self._records = records

    # pylint: disable-next=trivial-attribute-wrapper  # collecting records is this handler's entire purpose
    def emit(self, record: logging.LogRecord) -> None:
        """Append ``record`` to the collecting list."""
        self._records.append(record)


async def _join_room(
    conn_mgr: WebSocketConnectionManager,
    conn_id: str,
    websocket: RecordingWebSocket,
    workspace_id: str,
) -> None:
    """Register ``websocket`` and join it to ``workspace_id``'s room."""
    await conn_mgr.add_connection(conn_id, websocket)
    await conn_mgr.join_room(conn_id, _workspace_room(workspace_id))


async def _build_recipient(
    conn_mgr: WebSocketConnectionManager,
    workspace_id: str,
    kind: str,
    index: int,
) -> None:
    """Join one generated faulty recipient to ``workspace_id``."""
    websocket: RecordingWebSocket
    match kind:
        case "disconnect":
            websocket = DisconnectingWebSocket()
        case "timeout":
            websocket = BlockingWebSocket(started=asyncio.Event())
        case "unexpected":  # pragma: no branch - only generated kinds are matched
            websocket = FailingOnceWebSocket()
    await _join_room(
        conn_mgr, f"{workspace_id}-{kind}-{index}", websocket, workspace_id
    )


async def _build_rooms(
    conn_mgr: WebSocketConnectionManager,
    faults: dict[str, set[str]],
) -> tuple[dict[str, RecordingWebSocket], set[str]]:
    """Join a healthy recorder and the generated faulty recipients per workspace.

    Returns
    -------
    tuple[dict[str, RecordingWebSocket], set[str]]
        Healthy recorders by workspace, and the workspaces that received an
        unexpected-failure recipient.
    """
    healthy: dict[str, RecordingWebSocket] = {}
    rooms_with_unexpected_failure: set[str] = set()
    for workspace in WORKSPACES:
        recorder = RecordingWebSocket()
        healthy[workspace] = recorder
        await _join_room(conn_mgr, f"{workspace}-healthy", recorder, workspace)
        for index, kind in enumerate(sorted(faults[workspace])):
            await _build_recipient(conn_mgr, workspace, kind, index)
            if kind == "unexpected":
                rooms_with_unexpected_failure.add(workspace)
    return healthy, rooms_with_unexpected_failure


def _assert_healthy_deliveries(
    healthy: dict[str, RecordingWebSocket],
    events: list[str],
) -> None:
    """Require each healthy recipient to see its workspace's announcements once."""
    for workspace, recorder in healthy.items():
        expected = [
            {"seq": index}
            for index, event_workspace in enumerate(events)
            if event_workspace == workspace
        ]
        assert recorder.messages == expected, (
            f"healthy recipients in {workspace!r} must receive that workspace's "
            "announcements once, in order"
        )


def _assert_recovery(
    delays: list[float],
    records: list[logging.LogRecord],
    failed_rooms: set[str],
    events: list[str],
) -> None:
    """Require a restart, with one traceback-bearing error, per failed room."""
    expected_restarts = sum(1 for workspace in failed_rooms if workspace in events)
    assert delays == [RESTART_DELAY] * expected_restarts, (
        "only an unexpected failure may restart the consuming loop"
    )
    errors = [record for record in records if record.levelno >= logging.ERROR]
    assert len(errors) == expected_restarts, (
        "each unexpected failure must be reported exactly once"
    )
    assert all(error.exc_info is not None for error in errors), (
        "each unexpected failure must be reported with a traceback"
    )


async def _run_scenario(
    events: list[str],
    faults: dict[str, set[str]],
    records: list[logging.LogRecord],
) -> None:
    """Run the worker over ``events`` against the generated recipient faults."""
    conn_mgr = WebSocketConnectionManager()
    healthy, failed_rooms = await _build_rooms(conn_mgr, faults)

    scripted: list[tuple[str, dict[str, object]]] = [
        (workspace, {"seq": index}) for index, workspace in enumerate(events)
    ]
    feed = _ScriptedFeed(scripted)
    delays: list[float] = []

    def sleep(delay: float) -> cabc.Awaitable[None]:
        """Record the restart delay without waiting for it."""
        delays.append(delay)
        return asyncio.sleep(0)

    controller = WorkerController()
    await controller.start(
        announcement_worker,
        conn_mgr=conn_mgr,
        announcement_feed=typ.cast("AnnouncementFeed", feed),
        send_timeout=SEND_TIMEOUT,
        restart_delay=RESTART_DELAY,
        sleep=sleep,
    )
    try:
        async with asyncio.timeout(EVENT_WAIT_TIMEOUT):
            await feed.consumed.wait()
    finally:
        await controller.stop()

    _assert_healthy_deliveries(healthy, events)
    _assert_recovery(delays, records, failed_rooms, events)


@settings(
    deadline=None,
    max_examples=20,
    suppress_health_check=[HealthCheck.too_slow],
)
@given(
    events=st.lists(st.sampled_from(WORKSPACES), min_size=1, max_size=6),
    faults=st.fixed_dictionaries({
        workspace: st.sets(st.sampled_from(FAULT_KINDS), max_size=2)
        for workspace in WORKSPACES
    }),
)
def test_announcement_recovery_generated_faults(
    events: list[str],
    faults: dict[str, set[str]],
) -> None:
    """Expected faults never stop delivery; unexpected ones restart once per room."""
    records: list[logging.LogRecord] = []
    handler = _ListHandler(records)
    logger = logging.getLogger(WORKER_LOGGER)
    logger.addHandler(handler)
    try:
        asyncio.run(_run_scenario(events, faults, records))
    finally:
        logger.removeHandler(handler)
