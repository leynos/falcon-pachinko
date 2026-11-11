"""Regression tests for the reference announcement worker's failure policy."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import typing as typ

import pytest
import pytest_asyncio

from examples.reference_app.resources import _workspace_room
from examples.reference_app.services import AnnouncementFeed
from examples.reference_app.workers import announcement_worker
from falcon_pachinko.websocket import WebSocketConnectionManager
from falcon_pachinko.workers import WorkerController
from tests._stubs import (
    BlockingWebSocket,
    DisconnectingWebSocket,
    FailingOnceWebSocket,
    RecordingWebSocket,
    SignallingWebSocket,
    SnapshotTimeoutBackend,
)

if typ.TYPE_CHECKING:  # pragma: no cover - typing helpers
    import collections.abc as cabc

    from falcon_pachinko.protocols import WebSocketLike

EVENT_WAIT_TIMEOUT = 2.0
RESTART_DELAY = 0.25
SEND_TIMEOUT = 0.05
WORKER_LOGGER = "examples.reference_app.workers"


class _RecordingSleep:
    """Stand-in for ``asyncio.sleep`` that records the requested delays."""

    def __init__(self) -> None:
        """Initialise the recorder with no delays observed yet."""
        self.delays: list[float] = []

    # pylint: disable-next=trivial-attribute-wrapper  # recording the delay is this stub's entire purpose
    async def __call__(self, delay: float) -> None:
        """Record ``delay`` and return without waiting."""
        self.delays.append(delay)


async def _register(
    conn_mgr: WebSocketConnectionManager,
    conn_id: str,
    websocket: WebSocketLike,
    workspace_id: str,
) -> None:
    """Join ``conn_id`` to the room announcements for ``workspace_id`` use."""
    await conn_mgr.add_connection(conn_id, websocket)
    await conn_mgr.join_room(conn_id, _workspace_room(workspace_id))


async def _wait_for(event: asyncio.Event, description: str) -> None:
    """Wait for an event-gated assertion within the test timeout."""
    try:
        await asyncio.wait_for(event.wait(), timeout=EVENT_WAIT_TIMEOUT)
    except TimeoutError as exc:  # pragma: no cover - diagnostic only
        msg = f"timed out waiting for {description}"
        raise AssertionError(msg) from exc


async def _start_worker(
    controller: WorkerController,
    conn_mgr: WebSocketConnectionManager,
    feed: AnnouncementFeed,
    **context: object,
) -> asyncio.Task[None]:
    """Start the worker through the controller and return its task."""
    existing = asyncio.all_tasks()
    await controller.start(
        announcement_worker,
        conn_mgr=conn_mgr,
        announcement_feed=feed,
        **context,
    )
    (task,) = asyncio.all_tasks() - existing
    return task


def _error_records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    """Return the captured records logged at error level or above."""
    return [record for record in caplog.records if record.levelno >= logging.ERROR]


def _debug_messages(caplog: pytest.LogCaptureFixture) -> list[str]:
    """Return the worker's captured DEBUG messages, interpolated."""
    return [
        record.getMessage()
        for record in caplog.records
        if record.levelno == logging.DEBUG and record.name == WORKER_LOGGER
    ]


@pytest_asyncio.fixture
async def controller() -> cabc.AsyncIterator[WorkerController]:
    """Yield a controller and always stop it in teardown."""
    ctrl = WorkerController()
    try:
        yield ctrl
    finally:
        # Teardown must not mask a test failure with a second stop() error.
        with contextlib.suppress(Exception):
            await ctrl.stop()


@pytest.mark.asyncio
async def test_disconnect_keeps_announcements_flowing_to_other_workspaces(
    controller: WorkerController,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A disconnected recipient must not stop delivery to other rooms."""
    feed = AnnouncementFeed()
    conn_mgr = WebSocketConnectionManager()
    atlas_received = asyncio.Event()
    atlas_healthy = SignallingWebSocket(expected=2, received=atlas_received)
    zephyr_received = asyncio.Event()
    zephyr_healthy = SignallingWebSocket(expected=1, received=zephyr_received)
    await _register(conn_mgr, "atlas-healthy", atlas_healthy, "atlas")
    await _register(conn_mgr, "atlas-gone", DisconnectingWebSocket(), "atlas")
    await _register(conn_mgr, "zephyr-healthy", zephyr_healthy, "zephyr")

    caplog.set_level(logging.DEBUG, logger=WORKER_LOGGER)
    await controller.start(
        announcement_worker,
        conn_mgr=conn_mgr,
        announcement_feed=feed,
    )
    await feed.publish("atlas", {"seq": 1})
    await feed.publish("atlas", {"seq": 2})
    await feed.publish("zephyr", {"seq": 3})

    await _wait_for(atlas_received, "atlas announcements")
    await _wait_for(zephyr_received, "zephyr announcement")
    await controller.stop()

    assert atlas_healthy.messages == [{"seq": 1}, {"seq": 2}], (
        "the healthy atlas recipient must receive both announcements in order"
    )
    assert zephyr_healthy.messages == [{"seq": 3}], (
        "the zephyr recipient must still receive its announcement"
    )
    assert _error_records(caplog) == [], (
        "expected disconnects must not be logged as errors"
    )
    assert "Skipped 1 disconnected recipient(s)" in _debug_messages(caplog), (
        "a disconnect must be reported distinctly from a send timeout"
    )


@pytest.mark.asyncio
async def test_grouped_disconnects_keep_announcements_flowing(
    controller: WorkerController,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A grouped disconnect failure must be handled like a lone one."""
    feed = AnnouncementFeed()
    conn_mgr = WebSocketConnectionManager()
    atlas_received = asyncio.Event()
    atlas_healthy = SignallingWebSocket(expected=2, received=atlas_received)
    await _register(conn_mgr, "atlas-healthy", atlas_healthy, "atlas")
    await _register(conn_mgr, "atlas-gone-1", DisconnectingWebSocket(), "atlas")
    await _register(conn_mgr, "atlas-gone-2", DisconnectingWebSocket(), "atlas")

    caplog.set_level(logging.DEBUG, logger=WORKER_LOGGER)
    await controller.start(
        announcement_worker,
        conn_mgr=conn_mgr,
        announcement_feed=feed,
    )
    await feed.publish("atlas", {"seq": 1})
    await feed.publish("atlas", {"seq": 2})

    await _wait_for(atlas_received, "atlas announcements")
    await controller.stop()

    assert atlas_healthy.messages == [{"seq": 1}, {"seq": 2}], (
        "the healthy atlas recipient must receive both announcements in order"
    )
    assert _error_records(caplog) == [], (
        "grouped disconnects must not be logged as errors"
    )
    assert "Skipped 2 disconnected recipient(s)" in _debug_messages(caplog), (
        "a grouped disconnect must be reported once with its recipient count"
    )


@pytest.mark.asyncio
async def test_unresponsive_recipient_does_not_stall_the_consumer(
    controller: WorkerController,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A recipient that never accepts a send must not block the shared feed."""
    feed = AnnouncementFeed()
    conn_mgr = WebSocketConnectionManager()
    started = asyncio.Event()
    await _register(
        conn_mgr, "atlas-blocking", BlockingWebSocket(started=started), "atlas"
    )
    atlas_received = asyncio.Event()
    atlas_healthy = SignallingWebSocket(expected=1, received=atlas_received)
    await _register(conn_mgr, "atlas-healthy", atlas_healthy, "atlas")
    zephyr_received = asyncio.Event()
    zephyr_healthy = SignallingWebSocket(expected=1, received=zephyr_received)
    await _register(conn_mgr, "zephyr-healthy", zephyr_healthy, "zephyr")

    caplog.set_level(logging.DEBUG, logger=WORKER_LOGGER)
    await controller.start(
        announcement_worker,
        conn_mgr=conn_mgr,
        announcement_feed=feed,
        send_timeout=SEND_TIMEOUT,
    )
    await feed.publish("atlas", {"seq": 1})
    await feed.publish("zephyr", {"seq": 2})

    await _wait_for(atlas_received, "the healthy atlas recipient")
    await _wait_for(zephyr_received, "delivery after the send timeout")
    await controller.stop()

    assert atlas_healthy.messages == [{"seq": 1}], (
        "the healthy atlas recipient must still receive the announcement"
    )
    assert zephyr_healthy.messages == [{"seq": 2}], (
        "consumption must continue after an unresponsive recipient"
    )
    assert _error_records(caplog) == [], (
        "a send timeout must be treated as a best-effort delivery failure"
    )
    assert "Skipped 1 unresponsive recipient(s)" in _debug_messages(caplog), (
        "a send timeout must be reported distinctly from a disconnect"
    )


def _faulty_recipient(kind: str) -> RecordingWebSocket:
    """Build the faulty recipient double named by a generated fault kind."""
    if kind == "disconnect":
        return DisconnectingWebSocket()
    return BlockingWebSocket(started=asyncio.Event())


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("fault_kinds", "expected_debug"),
    [
        (("timeout", "timeout"), ("Skipped 2 unresponsive recipient(s)",)),
        (
            ("timeout", "disconnect"),
            (
                "Skipped 1 disconnected recipient(s)",
                "Skipped 1 unresponsive recipient(s)",
            ),
        ),
    ],
)
async def test_expected_failure_group_keeps_announcements_flowing(
    controller: WorkerController,
    caplog: pytest.LogCaptureFixture,
    fault_kinds: tuple[str, ...],
    expected_debug: tuple[str, ...],
) -> None:
    """A grouped expected failure must not stop later announcements."""
    feed = AnnouncementFeed()
    conn_mgr = WebSocketConnectionManager()
    atlas_received = asyncio.Event()
    atlas_healthy = SignallingWebSocket(expected=2, received=atlas_received)
    zephyr_received = asyncio.Event()
    zephyr_healthy = SignallingWebSocket(expected=1, received=zephyr_received)
    await _register(conn_mgr, "atlas-healthy", atlas_healthy, "atlas")
    for index, kind in enumerate(fault_kinds):
        await _register(
            conn_mgr, f"atlas-faulty-{index}", _faulty_recipient(kind), "atlas"
        )
    await _register(conn_mgr, "zephyr-healthy", zephyr_healthy, "zephyr")
    sleep = _RecordingSleep()

    caplog.set_level(logging.DEBUG, logger=WORKER_LOGGER)
    await controller.start(
        announcement_worker,
        conn_mgr=conn_mgr,
        announcement_feed=feed,
        send_timeout=SEND_TIMEOUT,
        restart_delay=RESTART_DELAY,
        sleep=sleep,
    )
    await feed.publish("atlas", {"seq": 1})
    await feed.publish("atlas", {"seq": 2})
    await feed.publish("zephyr", {"seq": 3})

    await _wait_for(atlas_received, "atlas announcements")
    await _wait_for(zephyr_received, "zephyr announcement")
    await controller.stop()

    assert atlas_healthy.messages == [{"seq": 1}, {"seq": 2}], (
        "the healthy atlas recipient must receive both announcements in order"
    )
    assert zephyr_healthy.messages == [{"seq": 3}], (
        "the zephyr recipient must receive its announcement after the expected group"
    )
    assert not sleep.delays, "expected failures must not restart the worker"
    assert _error_records(caplog) == [], (
        "an expected failure group must not be logged as an error"
    )
    messages = _debug_messages(caplog)
    for expected in expected_debug:
        assert expected in messages, (
            "each expected failure kind must be reported distinctly"
        )


@pytest.mark.asyncio
async def test_backend_snapshot_timeout_is_reported_and_worker_restarts(
    controller: WorkerController,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A backend timeout must reach supervision instead of skipping delivery."""
    feed = AnnouncementFeed()
    backend = SnapshotTimeoutBackend()
    conn_mgr = WebSocketConnectionManager(backend)
    zephyr_received = asyncio.Event()
    zephyr_healthy = SignallingWebSocket(expected=1, received=zephyr_received)
    await _register(conn_mgr, "zephyr-healthy", zephyr_healthy, "zephyr")
    sleep = _RecordingSleep()

    caplog.set_level(logging.DEBUG, logger=WORKER_LOGGER)
    await controller.start(
        announcement_worker,
        conn_mgr=conn_mgr,
        announcement_feed=feed,
        restart_delay=RESTART_DELAY,
        sleep=sleep,
    )
    await feed.publish("atlas", {"seq": 1})
    await feed.publish("zephyr", {"seq": 2})

    await _wait_for(zephyr_received, "delivery after the backend recovered")
    await controller.stop()

    assert zephyr_healthy.messages == [{"seq": 2}], (
        "the event dequeued when the backend failed must be dropped, not replayed"
    )
    assert sleep.delays == [RESTART_DELAY], (
        "a backend timeout must restart the consuming loop"
    )
    errors = _error_records(caplog)
    assert len(errors) == 1, "the backend timeout must be reported once"
    assert errors[0].exc_info is not None, "the failure must be logged with a traceback"
    assert isinstance(errors[0].exc_info[1], TimeoutError), (
        "the reported failure must be the backend timeout, not a recipient failure"
    )
    assert backend.snapshot_calls >= 2, (
        "the worker must take a snapshot for each attempted announcement"
    )


@pytest.mark.asyncio
async def test_configured_send_timeout_cancels_the_blocked_send(
    controller: WorkerController,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The configured deadline must cancel a blocked send before shutdown."""
    feed = AnnouncementFeed()
    conn_mgr = WebSocketConnectionManager()
    started = asyncio.Event()
    blocking = BlockingWebSocket(started=started)
    zephyr_received = asyncio.Event()
    zephyr_healthy = SignallingWebSocket(expected=1, received=zephyr_received)
    await _register(conn_mgr, "atlas-blocking", blocking, "atlas")
    await _register(conn_mgr, "zephyr-healthy", zephyr_healthy, "zephyr")

    caplog.set_level(logging.DEBUG, logger=WORKER_LOGGER)
    await controller.start(
        announcement_worker,
        conn_mgr=conn_mgr,
        announcement_feed=feed,
        send_timeout=SEND_TIMEOUT,
    )
    await feed.publish("atlas", {"seq": 1})
    await _wait_for(started, "the in-flight broadcast")
    # This proves the per-send deadline fired: the worker is still running, and
    # only ``wait_for`` can cancel the blocked send before ``stop()``.
    await _wait_for(blocking.cancelled, "the per-send deadline")
    await feed.publish("zephyr", {"seq": 2})
    await _wait_for(zephyr_received, "the announcement after the timeout")
    await controller.stop()

    assert not blocking.messages, "the blocked recipient must not record a delivery"
    assert zephyr_healthy.messages == [{"seq": 2}], (
        "consumption must continue after the configured deadline cancels a send"
    )
    assert _error_records(caplog) == [], (
        "a cancelled send must not be logged as an error"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("policy", "match"),
    [
        ({"send_timeout": -1.0}, "send_timeout must be positive"),
        ({"send_timeout": 0.0}, "send_timeout must be positive"),
        ({"send_timeout": math.nan}, "send_timeout must be positive"),
        ({"restart_delay": -0.5}, "restart_delay must not be negative"),
    ],
)
async def test_invalid_policy_is_rejected_at_startup(
    controller: WorkerController,
    policy: dict[str, float],
    match: str,
) -> None:
    """An invalid policy must fail startup instead of degrading delivery."""
    feed = AnnouncementFeed()
    conn_mgr = WebSocketConnectionManager()

    with pytest.raises(ValueError, match=match):
        await controller.start(
            announcement_worker,
            conn_mgr=conn_mgr,
            announcement_feed=feed,
            **policy,
        )


@pytest.mark.asyncio
async def test_unexpected_failure_is_logged_and_worker_restarts(
    controller: WorkerController,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An unexpected failure must be reported and consumption must resume."""
    feed = AnnouncementFeed()
    conn_mgr = WebSocketConnectionManager()
    failing = FailingOnceWebSocket()
    zephyr_received = asyncio.Event()
    zephyr_healthy = SignallingWebSocket(expected=1, received=zephyr_received)
    await _register(conn_mgr, "atlas-failing", failing, "atlas")
    await _register(conn_mgr, "zephyr-healthy", zephyr_healthy, "zephyr")
    sleep = _RecordingSleep()

    caplog.set_level(logging.DEBUG, logger=WORKER_LOGGER)
    await controller.start(
        announcement_worker,
        conn_mgr=conn_mgr,
        announcement_feed=feed,
        restart_delay=RESTART_DELAY,
        sleep=sleep,
    )
    await feed.publish("atlas", {"seq": 1})
    await feed.publish("zephyr", {"seq": 2})

    await _wait_for(zephyr_received, "delivery after the restart")
    await controller.stop()

    assert zephyr_healthy.messages == [{"seq": 2}], (
        "the zephyr recipient must receive the event queued after the failure"
    )
    assert not failing.messages, "the dequeued event must be dropped, not replayed"
    assert sleep.delays == [RESTART_DELAY], (
        "the worker must wait the configured restart delay"
    )
    errors = _error_records(caplog)
    assert len(errors) == 1, "the unexpected failure must be logged once"
    assert errors[0].exc_info is not None, "the failure must be logged with a traceback"


@pytest.mark.asyncio
async def test_mixed_group_reports_unexpected_leaf_and_resumes(
    controller: WorkerController,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An unexpected leaf beside a disconnect must still reach supervision."""
    feed = AnnouncementFeed()
    conn_mgr = WebSocketConnectionManager()
    zephyr_received = asyncio.Event()
    zephyr_healthy = SignallingWebSocket(expected=1, received=zephyr_received)
    await _register(conn_mgr, "atlas-gone", DisconnectingWebSocket(), "atlas")
    await _register(conn_mgr, "atlas-failing", FailingOnceWebSocket(), "atlas")
    await _register(conn_mgr, "zephyr-healthy", zephyr_healthy, "zephyr")
    sleep = _RecordingSleep()

    caplog.set_level(logging.DEBUG, logger=WORKER_LOGGER)
    await controller.start(
        announcement_worker,
        conn_mgr=conn_mgr,
        announcement_feed=feed,
        restart_delay=RESTART_DELAY,
        sleep=sleep,
    )
    await feed.publish("atlas", {"seq": 1})
    await feed.publish("zephyr", {"seq": 2})

    await _wait_for(zephyr_received, "delivery after the restart")
    await controller.stop()

    assert zephyr_healthy.messages == [{"seq": 2}], (
        "the zephyr recipient must receive the event queued after the failure"
    )
    assert sleep.delays == [RESTART_DELAY], (
        "the worker must wait the configured restart delay"
    )
    assert len(_error_records(caplog)) == 1, (
        "the mixed group must report exactly one unexpected failure"
    )


@pytest.mark.asyncio
async def test_stop_during_empty_feed_wait_is_cancelled_cleanly(
    controller: WorkerController,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Shutdown while waiting for an event must not be treated as a failure."""
    feed = AnnouncementFeed()
    conn_mgr = WebSocketConnectionManager()
    sleep = _RecordingSleep()

    caplog.set_level(logging.DEBUG, logger=WORKER_LOGGER)
    task = await _start_worker(
        controller,
        conn_mgr,
        feed,
        restart_delay=RESTART_DELAY,
        sleep=sleep,
    )
    # Yield once so the worker reaches the empty-feed wait before it is
    # cancelled; otherwise this would only prove that a never-run task stops.
    await asyncio.sleep(0)
    await controller.stop()

    assert task.done(), "the worker task must finish during shutdown"
    assert task.cancelled(), (
        "cancellation during the empty-feed wait must stop the worker"
    )
    assert not sleep.delays, "shutdown must not start a restart delay"
    assert _error_records(caplog) == [], "cancellation must not be logged as an error"


@pytest.mark.asyncio
async def test_stop_during_in_flight_broadcast_is_cancelled_cleanly(
    controller: WorkerController,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Shutdown mid-broadcast must cancel the send rather than restart it."""
    feed = AnnouncementFeed()
    conn_mgr = WebSocketConnectionManager()
    started = asyncio.Event()
    await _register(
        conn_mgr, "atlas-blocking", BlockingWebSocket(started=started), "atlas"
    )
    sleep = _RecordingSleep()

    caplog.set_level(logging.DEBUG, logger=WORKER_LOGGER)
    task = await _start_worker(
        controller,
        conn_mgr,
        feed,
        restart_delay=RESTART_DELAY,
        sleep=sleep,
    )
    await feed.publish("atlas", {"seq": 1})
    await _wait_for(started, "the in-flight broadcast")

    await controller.stop()

    assert task.done(), "the worker task must finish during shutdown"
    assert task.cancelled(), (
        "cancellation during the in-flight broadcast must stop the worker"
    )
    assert not sleep.delays, "shutdown must not start a restart delay"
    assert _error_records(caplog) == [], "cancellation must not be logged as an error"


@pytest.mark.asyncio
async def test_stop_during_restart_delay_is_cancelled_cleanly(
    controller: WorkerController,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Shutdown during the restart delay must stop the worker quietly."""
    feed = AnnouncementFeed()
    conn_mgr = WebSocketConnectionManager()
    await _register(conn_mgr, "atlas-failing", FailingOnceWebSocket(), "atlas")
    restart_started = asyncio.Event()

    async def blocking_sleep(delay: float) -> None:
        """Signal that the restart delay has begun, then wait for cancellation."""
        assert delay == RESTART_DELAY, "the worker must use the configured delay"
        restart_started.set()
        await asyncio.Event().wait()

    caplog.set_level(logging.DEBUG, logger=WORKER_LOGGER)
    task = await _start_worker(
        controller,
        conn_mgr,
        feed,
        restart_delay=RESTART_DELAY,
        sleep=blocking_sleep,
    )
    await feed.publish("atlas", {"seq": 1})
    await _wait_for(restart_started, "the restart delay")

    await controller.stop()

    assert task.done(), "the worker task must finish during shutdown"
    assert task.cancelled(), (
        "cancellation during the restart delay must stop the worker"
    )
    assert len(_error_records(caplog)) == 1, (
        "only the failure that triggered the restart may be reported"
    )
