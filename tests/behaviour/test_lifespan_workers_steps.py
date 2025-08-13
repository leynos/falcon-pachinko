"""Behavioural tests for lifespan-managed workers."""

from __future__ import annotations

import asyncio
import contextlib as cl
import typing as t

import falcon.asgi
from pytest_bdd import given, scenario, then, when

from falcon_pachinko.workers import WorkerController, worker

if t.TYPE_CHECKING:
    import collections.abc as cabc
    import contextlib as cl_typing


class LifespanApp(falcon.asgi.App):
    """Falcon ASGI App with a minimal lifespan decorator."""

    def __init__(self) -> None:
        super().__init__()
        self._lifespan_handler: (
            t.Callable[[falcon.asgi.App], cl_typing.AbstractAsyncContextManager[None]]
            | None
        ) = None

    def lifespan(
        self, fn: t.Callable[[falcon.asgi.App], cabc.AsyncIterator[None]]
    ) -> t.Callable[[falcon.asgi.App], cl_typing.AbstractAsyncContextManager[None]]:  # type: ignore[override]
        """Register a lifespan context manager."""
        manager = cl.asynccontextmanager(fn)
        self._lifespan_handler = manager
        return manager

    def lifespan_context(self) -> cl_typing.AbstractAsyncContextManager[None]:
        """Return the registered lifespan context manager."""
        if self._lifespan_handler is None:
            msg = "lifespan handler not set"
            raise RuntimeError(msg)
        return self._lifespan_handler(self)


@scenario("lifespan_workers.feature", "worker runs during lifespan")
def test_lifespan_worker() -> None:  # pragma: no cover - bdd registration
    """Scenario: worker runs during lifespan."""


@given("an app with a lifespan-managed worker", target_fixture="app_with_worker")
def create_app_with_worker() -> tuple[LifespanApp, dict[str, bool]]:
    """Create an app that starts a worker during lifespan."""
    app = LifespanApp()
    controller = WorkerController()
    state: dict[str, bool] = {"ran": False}

    @worker
    async def run_once(*, state: dict[str, bool]) -> None:
        state["ran"] = True

    @app.lifespan
    async def lifespan(app_instance) -> cabc.AsyncIterator[None]:  # noqa: ANN001
        await controller.start(run_once, state=state)
        yield
        await controller.stop()

    return app, state


@when("the app lifespan is executed")
def run_lifespan(app_with_worker: tuple[LifespanApp, dict[str, bool]]) -> None:
    """Run the application's lifespan context."""
    app, _ = app_with_worker

    async def _runner() -> None:
        async with app.lifespan_context():
            await asyncio.sleep(0)

    asyncio.run(_runner())


@then("the worker has run")
def worker_has_run(app_with_worker: tuple[LifespanApp, dict[str, bool]]) -> None:
    """Assert that the worker executed."""
    _, state = app_with_worker
    assert state["ran"] is True
