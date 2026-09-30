"""Unit tests for the WorkerController utility."""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import typing as typ
from unittest import mock

import pytest
import pytest_asyncio

from falcon_pachinko.workers import WorkerController, worker

EVENT_WAIT_TIMEOUT = 2.0

if typ.TYPE_CHECKING:  # pragma: no cover - used only for type checking
    import collections.abc as cabc

START_TIMEOUT = 1.0


async def _blocking_worker() -> None:
    """Remain pending until the controller cancels this worker."""
    await asyncio.Event().wait()


def _failing_factory() -> cabc.Coroutine[object, object, None]:
    """Raise while the controller is creating a worker coroutine."""
    msg = "factory failed"
    raise ValueError(msg)


def _capture_created_tasks(
    *,
    fail_on_call: int | None = None,
    error: BaseException | None = None,
) -> tuple[
    list[asyncio.Task[None]],
    cabc.Callable[[cabc.Coroutine[object, object, None]], asyncio.Task[None]],
]:
    """Return a task-creation spy and its captured tasks."""
    original_create_task = asyncio.create_task
    tasks: list[asyncio.Task[None]] = []
    call_count = 0

    def create_task(
        coroutine: cabc.Coroutine[object, object, None],
    ) -> asyncio.Task[None]:
        """Record delegated tasks and raise at the configured call."""
        nonlocal call_count
        call_count += 1
        if call_count == fail_on_call:
            if error is None:
                msg = "A task-creation error is required for the configured failure"
                raise AssertionError(msg)
            raise error

        task = original_create_task(coroutine)
        tasks.append(task)
        return task

    return tasks, create_task


@worker
async def _sample_worker(
    *, flag: dict[str, bool], ready: asyncio.Event | None = None
) -> None:
    """Set ``flag['ran']`` then block until cancelled."""
    flag["ran"] = True
    if ready is not None:
        ready.set()
    never_set = asyncio.Event()
    with contextlib.suppress(asyncio.CancelledError):
        await never_set.wait()


@worker
async def _failing_worker(*, ready: asyncio.Event | None = None) -> None:
    """Await once, then signal and raise an error deterministically."""
    await asyncio.sleep(0)
    if ready is not None:
        ready.set()
    raise RuntimeError("boom")


class _CleanupGate(typ.NamedTuple):
    worker: cabc.Callable[[], cabc.Coroutine[object, object, None]]
    started: asyncio.Event
    cleanup_started: asyncio.Event
    finish_cleanup: asyncio.Event
    cleaned_up: asyncio.Event


@pytest.fixture
def cleanup_gate() -> _CleanupGate:
    """Provide a worker whose cleanup waits for a test-controlled event."""
    started = asyncio.Event()
    cleanup_started = asyncio.Event()
    finish_cleanup = asyncio.Event()
    cleaned_up = asyncio.Event()

    async def blocking_worker() -> None:
        """Block until cancelled, then perform test-controlled cleanup."""
        started.set()
        try:
            await asyncio.wait_for(asyncio.Event().wait(), timeout=EVENT_WAIT_TIMEOUT)
        finally:
            cleanup_started.set()
            await asyncio.wait_for(finish_cleanup.wait(), timeout=EVENT_WAIT_TIMEOUT)
            cleaned_up.set()

    return _CleanupGate(
        blocking_worker, started, cleanup_started, finish_cleanup, cleaned_up
    )


async def assert_controller_restarts(controller: WorkerController) -> None:
    """Assert a stopped controller can start and stop a healthy worker.

    Parameters
    ----------
    controller : WorkerController
        A controller whose previous task set has been cleared.
    """
    restarted = asyncio.Event()

    async def healthy_worker() -> None:
        """Signal startup and wait until the controller stops the worker."""
        restarted.set()
        await asyncio.wait_for(asyncio.Event().wait(), timeout=EVENT_WAIT_TIMEOUT)

    await controller.start(healthy_worker)
    await asyncio.wait_for(restarted.wait(), timeout=EVENT_WAIT_TIMEOUT)
    await controller.stop()


@pytest_asyncio.fixture
async def controller() -> cabc.AsyncIterator[WorkerController]:
    """Yield a controller and always stop it in teardown."""
    ctrl = WorkerController()
    try:
        yield ctrl
    finally:
        # Avoid teardown-time errors leaking into unrelated tests
        with contextlib.suppress(Exception):
            await ctrl.stop()


@pytest.mark.asyncio
async def test_worker_controller_runs_and_stops(
    controller: WorkerController,
) -> None:
    """Start and stop a worker, verifying context injection."""
    flag: dict[str, bool] = {}
    ready = asyncio.Event()
    await controller.start(_sample_worker, flag=flag, ready=ready)
    # start() schedules tasks but does not run them.
    await asyncio.wait_for(ready.wait(), timeout=START_TIMEOUT)
    assert flag["ran"] is True, "worker must run and set the flag before being stopped"
    await controller.stop()


@pytest.mark.asyncio
async def test_start_twice_raises_error(controller: WorkerController) -> None:
    """Starting twice without stopping should raise an error."""
    flag: dict[str, bool] = {}
    await controller.start(_sample_worker, flag=flag)
    with pytest.raises(RuntimeError):
        await controller.start(_sample_worker, flag=flag)
    await controller.stop()


@pytest.mark.asyncio
async def test_stop_is_idempotent(controller: WorkerController) -> None:
    """Stopping multiple times should not raise."""
    flag: dict[str, bool] = {}
    await controller.start(_sample_worker, flag=flag)
    await asyncio.sleep(0)
    await controller.stop()
    await controller.stop()


@pytest.mark.asyncio
async def test_exception_propagates_on_stop(
    controller: WorkerController,
) -> None:
    """Exceptions raised by workers should surface when stopping."""
    ready = asyncio.Event()
    await controller.start(_failing_worker, ready=ready)
    # Wait until the worker has run past its internal sleep and is about to raise
    await ready.wait()
    with pytest.raises(RuntimeError, match="boom"):
        await controller.stop()


@pytest.mark.asyncio
async def test_stop_waits_for_cleanup_and_controller_can_restart(
    controller: WorkerController,
    cleanup_gate: _CleanupGate,
) -> None:
    """Wait for cancellation cleanup before restarting the controller.

    Parameters
    ----------
    controller : WorkerController
        The controller under test.
    cleanup_gate : _CleanupGate
        A worker and events that let the test hold cleanup until released.
    """
    await controller.start(cleanup_gate.worker)
    await asyncio.wait_for(cleanup_gate.started.wait(), timeout=EVENT_WAIT_TIMEOUT)

    stopping = asyncio.create_task(controller.stop())
    await asyncio.wait_for(
        cleanup_gate.cleanup_started.wait(), timeout=EVENT_WAIT_TIMEOUT
    )
    cleanup_gate.finish_cleanup.set()
    await stopping

    assert cleanup_gate.cleaned_up.is_set(), (
        "stop must wait for asynchronous worker cleanup"
    )
    await controller.stop()
    await assert_controller_restarts(controller)


@pytest.mark.asyncio
async def test_failure_propagates_after_peer_cleanup_and_allows_restart(
    controller: WorkerController,
    cleanup_gate: _CleanupGate,
) -> None:
    """Propagate worker failure only after peer cleanup and allow restart.

    Parameters
    ----------
    controller : WorkerController
        The controller under test.
    cleanup_gate : _CleanupGate
        A peer worker and events that let the test hold cleanup until released.
    """
    failure = RuntimeError("worker failed")
    failure_ready = asyncio.Event()
    fail_now = asyncio.Event()
    failed = asyncio.Event()

    async def failing_worker() -> None:
        """Wait for permission to fail, then signal and raise the worker error."""
        failure_ready.set()
        await asyncio.wait_for(fail_now.wait(), timeout=EVENT_WAIT_TIMEOUT)
        failed.set()
        raise failure

    await controller.start(failing_worker, cleanup_gate.worker)
    await asyncio.wait_for(failure_ready.wait(), timeout=EVENT_WAIT_TIMEOUT)
    fail_now.set()
    await asyncio.wait_for(failed.wait(), timeout=EVENT_WAIT_TIMEOUT)
    await asyncio.wait_for(cleanup_gate.started.wait(), timeout=EVENT_WAIT_TIMEOUT)

    stopping = asyncio.create_task(controller.stop())
    await asyncio.wait_for(
        cleanup_gate.cleanup_started.wait(), timeout=EVENT_WAIT_TIMEOUT
    )
    cleanup_gate.finish_cleanup.set()

    with pytest.raises(RuntimeError) as raised:
        await stopping

    assert raised.value is failure, "stop must raise the original worker exception"
    assert cleanup_gate.cleaned_up.is_set(), "stop must wait for peer cleanup"
    await assert_controller_restarts(controller)


@pytest.mark.asyncio
async def test_stop_raises_first_exception_in_registration_order(
    controller: WorkerController,
) -> None:
    """Select by registration order when the later worker fails first.

    Parameters
    ----------
    controller : WorkerController
        The controller under test.
    """
    first_failure = ValueError("first registered worker")
    second_failure = TypeError("second registered worker")
    allow_second_failure = asyncio.Event()
    second_failed = asyncio.Event()
    first_failed = asyncio.Event()

    async def first_worker() -> None:
        """Wait for the later worker to fail, then raise the first error."""
        await asyncio.wait_for(second_failed.wait(), timeout=EVENT_WAIT_TIMEOUT)
        first_failed.set()
        raise first_failure

    async def second_worker() -> None:
        """Wait for permission to fail before the first-registered worker."""
        await asyncio.wait_for(allow_second_failure.wait(), timeout=EVENT_WAIT_TIMEOUT)
        second_failed.set()
        raise second_failure

    await controller.start(first_worker, second_worker)
    allow_second_failure.set()
    await asyncio.wait_for(first_failed.wait(), timeout=EVENT_WAIT_TIMEOUT)
    await asyncio.wait_for(second_failed.wait(), timeout=EVENT_WAIT_TIMEOUT)

    with pytest.raises(ValueError, match="first registered worker") as raised:
        await controller.stop()

    assert raised.value is first_failure, (
        "stop must raise the first registered worker's exception"
    )

@pytest.mark.asyncio
async def test_start_factory_failure_rolls_back_and_allows_restart(
    controller: WorkerController,
) -> None:
    """Roll back scheduled tasks and allow a later start after factory failure."""
    tasks, create_task = _capture_created_tasks()

    with (
        mock.patch.object(asyncio, "create_task", create_task),
        pytest.raises(ValueError, match="factory failed"),
    ):
        await controller.start(_blocking_worker, _failing_factory)

    assert len(tasks) == 1, (
        "the first worker should be scheduled before factory failure"
    )
    assert all(task.done() and task.cancelled() for task in tasks), (
        "every task from partial startup should be cancelled and complete"
    )
    assert controller._tasks == [], "failed startup should clear the task list"

    await controller.start(_blocking_worker)
    await controller.stop()


@pytest.mark.asyncio
async def test_start_task_creation_failure_closes_rejected_coroutine(
    controller: WorkerController,
) -> None:
    """Close a worker coroutine rejected by task creation and roll back."""
    coroutines: list[cabc.Coroutine[object, object, None]] = []

    def second_worker() -> cabc.Coroutine[object, object, None]:
        """Return a coroutine so the test can inspect its cleanup state."""
        coroutine = _blocking_worker()
        coroutines.append(coroutine)
        return coroutine

    tasks, create_task = _capture_created_tasks(
        fail_on_call=2,
        error=RuntimeError("scheduling failed"),
    )

    with (
        mock.patch.object(asyncio, "create_task", create_task),
        pytest.raises(RuntimeError, match="scheduling failed"),
    ):
        await controller.start(_blocking_worker, second_worker)

    assert len(coroutines) == 1, "the rejected worker coroutine should be retained"
    assert inspect.getcoroutinestate(coroutines[0]) == inspect.CORO_CLOSED, (
        "task-creation failure should close the rejected coroutine"
    )
    assert len(tasks) == 1, "only the first worker should become a task"
    assert all(task.done() and task.cancelled() for task in tasks), (
        "every task from partial startup should be cancelled and complete"
    )
    assert controller._tasks == [], "failed startup should clear the task list"
