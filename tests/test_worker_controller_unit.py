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

if typ.TYPE_CHECKING:  # pragma: no cover - used only for type checking
    import collections.abc as cabc


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
async def _sample_worker(*, flag: dict[str, bool]) -> None:
    """Set ``flag['ran']`` then block until cancelled."""
    flag["ran"] = True
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
    await controller.start(_sample_worker, flag=flag)
    await asyncio.sleep(0)
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
    assert controller._stack is None, "failed startup should clear the exit stack"

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
    assert controller._stack is None, "failed startup should clear the exit stack"


@pytest.mark.asyncio
async def test_start_factory_failure_closes_async_exit_stack_once(
    controller: WorkerController,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Close the async exit stack once when worker creation fails."""
    original_aclose = contextlib.AsyncExitStack.aclose
    close_count = 0

    async def count_aclose(stack: contextlib.AsyncExitStack) -> None:
        """Count stack cleanup while preserving its original behaviour."""
        nonlocal close_count
        close_count += 1
        await original_aclose(stack)

    monkeypatch.setattr(contextlib.AsyncExitStack, "aclose", count_aclose)

    with pytest.raises(ValueError, match="factory failed"):
        await controller.start(_failing_factory)

    assert close_count == 1, "failed startup should close the exit stack exactly once"
    assert controller._stack is None, "failed startup should clear the exit stack"
