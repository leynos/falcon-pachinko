"""Unit tests for the WorkerController utility."""

from __future__ import annotations

import asyncio
import contextlib
import typing as typ

import pytest
import pytest_asyncio

from falcon_pachinko.workers import WorkerController, worker

if typ.TYPE_CHECKING:  # pragma: no cover - used only for type checking
    import collections.abc as cabc


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
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleanup_started.set()
            await finish_cleanup.wait()
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
        restarted.set()
        await asyncio.Event().wait()

    await controller.start(healthy_worker)
    await restarted.wait()
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
    await cleanup_gate.started.wait()

    stopping = asyncio.create_task(controller.stop())
    await cleanup_gate.cleanup_started.wait()
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
        failure_ready.set()
        await fail_now.wait()
        failed.set()
        raise failure

    await controller.start(failing_worker, cleanup_gate.worker)
    await failure_ready.wait()
    fail_now.set()
    await failed.wait()
    await cleanup_gate.started.wait()

    stopping = asyncio.create_task(controller.stop())
    await cleanup_gate.cleanup_started.wait()
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
        await second_failed.wait()
        first_failed.set()
        raise first_failure

    async def second_worker() -> None:
        await allow_second_failure.wait()
        second_failed.set()
        raise second_failure

    await controller.start(first_worker, second_worker)
    allow_second_failure.set()
    await first_failed.wait()
    await second_failed.wait()

    with pytest.raises(ValueError, match="first registered worker") as raised:
        await controller.stop()

    assert raised.value is first_failure, (
        "stop must raise the first registered worker's exception"
    )
