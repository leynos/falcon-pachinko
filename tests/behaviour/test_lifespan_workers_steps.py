"""Behavioural tests for lifespan-managed workers."""

from __future__ import annotations

import asyncio
import typing as typ

from pytest_bdd import given, scenario, then, when

from falcon_pachinko.workers import WorkerController, worker
from tests.behaviour._lifespan import LifespanApp

START_TIMEOUT = 2.0
AppWithWorker = tuple[LifespanApp, dict[str, bool], asyncio.Event, asyncio.Event]

if typ.TYPE_CHECKING:
    import collections.abc as cabc


@scenario("lifespan_workers.feature", "worker runs during lifespan")
def test_lifespan_worker() -> None:  # pragma: no cover - bdd registration
    """Scenario: worker runs during lifespan."""


@given("an app with a lifespan-managed worker", target_fixture="app_with_worker")
def create_app_with_worker() -> AppWithWorker:
    """Create an app that starts a worker during lifespan."""
    app = LifespanApp()
    controller = WorkerController()
    state: dict[str, bool] = {"ran": False}
    started = asyncio.Event()
    stopped = asyncio.Event()

    @worker
    async def run_until_cancelled(
        *, state: dict[str, bool], started: asyncio.Event, stopped: asyncio.Event
    ) -> None:
        state["ran"] = True
        started.set()
        try:
            await asyncio.Future()
        finally:
            stopped.set()

    @app.lifespan
    async def lifespan(app_instance: LifespanApp) -> cabc.AsyncIterator[None]:
        await controller.start(
            run_until_cancelled, state=state, started=started, stopped=stopped
        )
        yield
        await controller.stop()

    return app, state, started, stopped


@when("the app lifespan is executed")
def run_lifespan(app_with_worker: AppWithWorker) -> None:
    """Run the application's lifespan context."""
    app, _, started, _ = app_with_worker

    async def _runner() -> None:
        async with app.lifespan_context():
            await asyncio.wait_for(started.wait(), timeout=START_TIMEOUT)

    asyncio.run(_runner())


@then("the worker has run")
def worker_has_run(app_with_worker: AppWithWorker) -> None:
    """Assert that the worker executed."""
    _, state, _, _ = app_with_worker
    assert state["ran"] is True


@then("the worker stops after the lifespan ends")
def worker_stops(app_with_worker: AppWithWorker) -> None:
    """Assert that the worker stopped after the lifespan context."""
    _, _, _, stopped = app_with_worker
    assert stopped.is_set()
