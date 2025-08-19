"""Tests for the WorkerController and worker decorator."""

from __future__ import annotations

import asyncio
import typing as typ

import pytest
import pytest_asyncio

from falcon_pachinko.workers import WorkerController, WorkerFn, worker

if typ.TYPE_CHECKING:  # pragma: no cover - used only for type checking
    import collections.abc as cabc


@pytest_asyncio.fixture
async def controller() -> cabc.AsyncIterator[WorkerController]:
    """Yield a fresh controller and ensure it is stopped afterwards."""
    ctrl = WorkerController()
    try:
        yield ctrl
    finally:
        # ``stop`` is idempotent, so calling twice is safe even if tests stop it
        await ctrl.stop()


@pytest.fixture
def noop_worker() -> WorkerFn:
    """Provide a simple worker that immediately yields control."""

    async def noop() -> None:
        await asyncio.sleep(0)

    return noop


@worker
async def _logging_worker(
    *, log: list[str], started: asyncio.Event, stopped: asyncio.Event
) -> None:
    """Append ticks to *log* until cancelled."""
    started.set()
    try:
        while True:
            log.append("tick")
            await asyncio.sleep(0)
    except asyncio.CancelledError:
        stopped.set()
        raise


@pytest.mark.asyncio
async def test_start_and_stop_runs_workers(controller: WorkerController) -> None:
    """Verify workers start, receive context, and are cancelled on stop."""
    log: list[str] = []
    started = asyncio.Event()
    stopped = asyncio.Event()
    await controller.start(_logging_worker, log=log, started=started, stopped=stopped)
    await asyncio.wait_for(started.wait(), 0.1)
    assert log
    await controller.stop()
    assert stopped.is_set()


@pytest.mark.asyncio
async def test_stop_propagates_worker_exception(controller: WorkerController) -> None:
    """Exceptions raised by workers should bubble up when stopping."""

    async def boom() -> None:
        raise RuntimeError("boom")

    await controller.start(boom)
    await asyncio.sleep(0)  # Let the task run and fail
    with pytest.raises(RuntimeError, match="boom"):
        await controller.stop()


@pytest.mark.asyncio
async def test_start_twice_errors_and_restart_allowed(
    controller: WorkerController, noop_worker: WorkerFn
) -> None:
    """Starting twice without stopping raises, but restart after stop is OK."""
    await controller.start(noop_worker)
    with pytest.raises(RuntimeError):
        await controller.start(noop_worker)
    await controller.stop()

    # Controller can be restarted after a clean stop
    await controller.start(noop_worker)
    await controller.stop()


@pytest.mark.asyncio
async def test_stop_is_idempotent(
    controller: WorkerController, noop_worker: WorkerFn
) -> None:
    """Calling stop multiple times should be a no-op after the first."""
    # Stop before start should not error
    await controller.stop()

    await controller.start(noop_worker)
    await controller.stop()
    # Second stop call should return immediately
    await controller.stop()
