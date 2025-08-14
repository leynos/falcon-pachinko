"""Unit tests for the WorkerController utility."""

from __future__ import annotations

import asyncio
import typing

import pytest
import pytest_asyncio

from falcon_pachinko.workers import WorkerController, worker

if typing.TYPE_CHECKING:  # pragma: no cover - used only for type checking
    import collections.abc as cabc


@worker
async def _sample_worker(*, flag: dict[str, bool]) -> None:
    flag["ran"] = True
    try:
        while True:
            await asyncio.sleep(0)
    except asyncio.CancelledError:
        pass


@worker
async def _failing_worker() -> None:
    """Yield once before raising an error."""
    await asyncio.sleep(0)
    raise RuntimeError("boom")


@pytest_asyncio.fixture
async def controller() -> cabc.AsyncIterator[WorkerController]:
    """Yield a controller and always stop it in teardown."""
    ctrl = WorkerController()
    try:
        yield ctrl
    finally:
        await ctrl.stop()


@pytest.mark.asyncio
async def test_worker_controller_runs_and_stops(
    controller: WorkerController,
) -> None:
    """Start and stop a worker, verifying context injection."""
    flag: dict[str, bool] = {}
    await controller.start(_sample_worker, flag=flag)
    await asyncio.sleep(0)
    assert flag["ran"] is True
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
    await controller.start(_failing_worker)
    # Yield twice so the worker runs past its internal sleep and raises
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    with pytest.raises(RuntimeError, match="boom"):
        await controller.stop()
