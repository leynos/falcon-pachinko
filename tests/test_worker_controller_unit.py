"""Unit tests for the WorkerController utility."""

from __future__ import annotations

import asyncio

import pytest

from falcon_pachinko.workers import WorkerController, worker


@worker
async def _sample_worker(*, flag: dict[str, bool]) -> None:
    flag["ran"] = True
    try:
        while True:
            await asyncio.sleep(0)
    except asyncio.CancelledError:
        pass


@pytest.mark.asyncio
async def test_worker_controller_runs_and_stops() -> None:
    """Start and stop a worker, verifying context injection."""
    flag: dict[str, bool] = {}
    controller = WorkerController()
    await controller.start(_sample_worker, flag=flag)
    await asyncio.sleep(0)
    assert flag["ran"] is True
    await controller.stop()
