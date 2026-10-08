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


def test_worker_returns_same_function_and_sets_marker() -> None:
    """Verify decoration marks and returns the original function."""

    async def fn() -> None:
        """Provide a worker function for the decorator assertion."""

    decorated_fn = worker(fn)

    assert decorated_fn is fn, "the decorator should return the original function"
    assert getattr(fn, "__pachinko_worker__", False) is True, (
        "the decorator should set the worker marker"
    )


def test_undecorated_async_function_has_no_marker() -> None:
    """Verify an undecorated async function has no worker marker."""

    async def fn() -> None:
        """Provide an undecorated worker function."""

    assert getattr(fn, "__pachinko_worker__", False) is False, (
        "an undecorated function should not have the worker marker"
    )


@pytest.mark.parametrize(
    "worker_style",
    ["decorated", "undecorated"],
)
@pytest.mark.asyncio
async def test_start_and_stop_runs_decorated_or_undecorated_worker(
    controller: WorkerController, worker_style: str
) -> None:
    """Verify either worker form receives context and is cancelled on stop."""
    received_context: dict[str, object] = {}
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def run_worker(
        *, label: str, started: asyncio.Event, cancelled: asyncio.Event
    ) -> None:
        """Capture supplied context, then wait until the controller stops it."""
        received_context.update(
            label=label,
            started=started,
            cancelled=cancelled,
        )
        started.set()
        try:
            await asyncio.Future[None]()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    selected_worker = worker(run_worker) if worker_style == "decorated" else run_worker
    await controller.start(
        selected_worker,
        label="background worker",
        started=started,
        cancelled=cancelled,
    )
    await asyncio.wait_for(started.wait(), 0.1)
    assert received_context == {
        "label": "background worker",
        "started": started,
        "cancelled": cancelled,
    }, "the worker should receive every context keyword argument"
    assert started.is_set(), "the worker should run after start schedules it"

    await controller.stop()

    assert cancelled.is_set(), "stop should cancel the worker and let it clean up"


@pytest.mark.asyncio
async def test_stop_propagates_worker_exception(controller: WorkerController) -> None:
    """Exceptions raised by workers should bubble up when stopping."""

    async def boom() -> None:
        await asyncio.sleep(0)
        raise RuntimeError("boom")

    await controller.start(boom)
    # Two scheduler ticks: one to run the worker up to its await, one to let
    # it resume and raise.
    await asyncio.sleep(0)
    await asyncio.sleep(0)
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
