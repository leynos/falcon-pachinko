"""Unit tests for the WorkerController utility."""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import typing as typ
from unittest import mock

import pytest
import pytest_asyncio
from hypothesis import given
from hypothesis import strategies as st

from falcon_pachinko.workers import WorkerController, WorkerFn, worker

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
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Close a worker coroutine rejected by task creation and roll back."""
    coroutines: list[cabc.Coroutine[object, object, None]] = []
    scheduling_error = RuntimeError("scheduling failed")

    def second_worker() -> cabc.Coroutine[object, object, None]:
        """Return a coroutine so the test can inspect its cleanup state."""
        coroutine = _blocking_worker()
        coroutines.append(coroutine)
        return coroutine

    tasks, create_task = _capture_created_tasks(
        fail_on_call=2,
        error=scheduling_error,
    )
    with monkeypatch.context() as patch:
        patch.setattr(asyncio, "create_task", create_task)
        with pytest.raises(
            RuntimeError,
            match="scheduling failed",
        ) as exception_info:
            await controller.start(_blocking_worker, second_worker)

    assert exception_info.value is scheduling_error, (
        "task-creation failure should preserve the original exception object"
    )
    assert len(coroutines) == 1, "the rejected worker coroutine should be retained"
    assert inspect.getcoroutinestate(coroutines[0]) == inspect.CORO_CLOSED, (
        "task-creation failure should close the rejected coroutine"
    )
    assert len(tasks) == 1, "only the first worker should become a task"
    assert all(task.done() and task.cancelled() for task in tasks), (
        "every task from partial startup should be cancelled and complete"
    )
    assert controller._tasks == [], "failed startup should clear the task list"


@pytest.mark.asyncio
async def test_start_preserves_error_when_rollback_fails(
    controller: WorkerController,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keep the startup error primary when rollback itself fails."""
    startup_error = ValueError("factory failed")
    rollback_error = RuntimeError("rollback failed")

    def failing_factory() -> cabc.Coroutine[object, object, None]:
        """Raise the retained startup exception object."""
        raise startup_error

    async def fail_rollback(_controller: WorkerController) -> None:
        """Raise a secondary cleanup error for the test."""
        await asyncio.sleep(0)
        raise rollback_error

    monkeypatch.setattr(WorkerController, "_rollback_start", fail_rollback)

    with pytest.raises(ValueError, match="factory failed") as exception_info:
        await controller.start(failing_factory)

    assert exception_info.value is startup_error, (
        "rollback failure should not replace the startup exception"
    )
    assert any("rollback failed" in note for note in startup_error.__notes__), (
        "the startup exception should retain the rollback failure as a note"
    )


@pytest.mark.asyncio
async def test_start_finishes_task_cleanup_when_cancelled_during_rollback(
    controller: WorkerController,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Finish task cleanup and preserve startup failure after cancellation."""
    startup_error = ValueError("factory failed")
    cleanup_started = asyncio.Event()
    allow_cleanup = asyncio.Event()
    tasks, create_task = _capture_created_tasks()
    original_create_task = asyncio.create_task

    async def cooperative_worker() -> None:
        """Hold cancellation cleanup until the test releases it."""
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cleanup_started.set()
            await allow_cleanup.wait()
            raise

    def failing_factory() -> cabc.Coroutine[object, object, None]:
        """Raise the retained startup exception object."""
        raise startup_error

    monkeypatch.setattr(asyncio, "create_task", create_task)
    start_task = original_create_task(
        controller.start(cooperative_worker, failing_factory)
    )

    await asyncio.wait_for(cleanup_started.wait(), timeout=EVENT_WAIT_TIMEOUT)
    start_task.cancel()
    await asyncio.sleep(0)
    allow_cleanup.set()

    with pytest.raises(ValueError, match="factory failed") as exception_info:
        await start_task

    assert exception_info.value is startup_error, (
        "cancellation during rollback should preserve the startup exception"
    )
    assert any("also cancelled" in note for note in startup_error.__notes__), (
        "the startup exception should record cancellation during rollback"
    )
    assert len(tasks) == 1, "startup should have scheduled one worker before failure"
    assert tasks[0].done(), "rollback should finish awaiting task cancellation"
    assert tasks[0].cancelled(), "rollback should cancel the scheduled worker"
    assert controller._tasks == [], "cancelled startup should clear the task list"


@pytest.mark.asyncio
async def test_rollback_waits_for_tasks_after_cancellation(
    controller: WorkerController,
) -> None:
    """Finish awaiting worker cancellation before rollback returns."""
    worker_started = asyncio.Event()
    cancellation_started = asyncio.Event()
    finish_cancellation = asyncio.Event()

    async def cooperative_worker() -> None:
        """Hold cancellation cleanup until the test releases it."""
        worker_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancellation_started.set()
            await finish_cancellation.wait()
            raise

    await controller.start(cooperative_worker)
    await asyncio.wait_for(worker_started.wait(), timeout=1)
    worker_task = controller._tasks[0]
    rollback_task = asyncio.create_task(controller._rollback_start())

    await asyncio.wait_for(cancellation_started.wait(), timeout=1)
    rollback_task.cancel()

    assert not rollback_task.done(), (
        "rollback should keep waiting after its caller is cancelled"
    )
    finish_cancellation.set()

    rollback_error = await rollback_task
    assert isinstance(rollback_error, asyncio.CancelledError), (
        "rollback should retain cancellation while finishing cleanup"
    )

    assert worker_task.done(), "rollback should finish awaiting worker completion"
    assert worker_task.cancelled(), "rollback should cancel the worker task"
    assert controller._tasks == [], "rollback should clear the task list"


@given(
    scheduled_worker_count=st.integers(min_value=0, max_value=4),
    failure_source=st.integers(min_value=0, max_value=1),
)
def test_start_failure_rolls_back_bounded_worker_sequences(
    scheduled_worker_count: int,
    failure_source: int,
) -> None:
    """Roll back every bounded worker prefix for both failure sources."""

    async def verify() -> None:
        """Exercise one generated startup failure and verify its rollback."""
        controller = WorkerController()
        tasks: list[asyncio.Task[None]]
        workers: list[WorkerFn]
        rejected_coroutines: list[cabc.Coroutine[object, object, None]] = []
        if failure_source == 1:
            startup_error = RuntimeError("scheduling failed")

            def rejected_worker() -> cabc.Coroutine[object, object, None]:
                """Return the coroutine rejected at the generated position."""
                coroutine = _blocking_worker()
                rejected_coroutines.append(coroutine)
                return coroutine

            tasks, create_task = _capture_created_tasks(
                fail_on_call=scheduled_worker_count + 1,
                error=startup_error,
            )
            workers = [_blocking_worker for _ in range(scheduled_worker_count)]
            workers.append(rejected_worker)
        else:
            startup_error = ValueError("factory failed")

            def failing_factory() -> cabc.Coroutine[object, object, None]:
                """Raise the generated factory error."""
                raise startup_error

            tasks, create_task = _capture_created_tasks()
            workers = [_blocking_worker for _ in range(scheduled_worker_count)]
            workers.append(failing_factory)

        with (
            mock.patch.object(asyncio, "create_task", create_task),
            pytest.raises(type(startup_error)) as exception_info,
        ):
            await controller.start(*workers)

        assert exception_info.value is startup_error, (
            "startup should re-raise the generated exception object"
        )
        assert len(tasks) == scheduled_worker_count, (
            "only workers before the failure position should be scheduled"
        )
        assert all(task.done() and task.cancelled() for task in tasks), (
            "every scheduled worker should be cancelled and awaited"
        )
        assert not controller._tasks, "failed startup should reset scheduled tasks"
        if failure_source == 1:
            assert len(rejected_coroutines) == 1, (
                "task-creation failure should retain the rejected coroutine"
            )
            assert inspect.getcoroutinestate(rejected_coroutines[0]) == (
                inspect.CORO_CLOSED
            ), "task-creation failure should close the rejected coroutine"

        await controller.start(_blocking_worker)
        await controller.stop()

    asyncio.run(verify())
