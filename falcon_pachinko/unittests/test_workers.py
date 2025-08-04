"""Tests for the WorkerController and worker decorator."""

from __future__ import annotations

import asyncio

import pytest

from falcon_pachinko.workers import WorkerController, worker


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
async def test_start_and_stop_runs_workers() -> None:
    """Verify workers start, receive context, and are cancelled on stop."""
    controller = WorkerController()
    log: list[str] = []
    started = asyncio.Event()
    stopped = asyncio.Event()
    await controller.start(_logging_worker, log=log, started=started, stopped=stopped)
    await asyncio.wait_for(started.wait(), 0.1)
    assert log
    await controller.stop()
    assert stopped.is_set()


@pytest.mark.asyncio
async def test_stop_propagates_worker_exception() -> None:
    """Exceptions raised by workers should bubble up when stopping."""
    controller = WorkerController()

    async def boom() -> None:
        raise RuntimeError("boom")

    await controller.start(boom)
    await asyncio.sleep(0)  # Let the task run and fail
    with pytest.raises(RuntimeError, match="boom"):
        await controller.stop()
