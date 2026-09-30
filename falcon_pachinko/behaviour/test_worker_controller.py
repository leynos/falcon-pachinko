"""Behavioural tests for the WorkerController lifecycle."""

from __future__ import annotations

import asyncio
import typing as typ
from unittest import mock

if typ.TYPE_CHECKING:
    import collections.abc as cabc

import pytest
from pytest_bdd import given, scenario, then, when

from falcon_pachinko.workers import WorkerController


@scenario("features/worker_controller.feature", "Run a background worker")
def test_run_worker() -> None:
    """Scenario: Run a background worker."""


@scenario(
    "features/worker_controller.feature",
    "Roll back partial startup and permit retry",
)
def test_failed_start_allows_retry() -> None:
    """Scenario: Roll back partial startup and permit retry."""


@pytest.fixture
def context() -> dict[str, typ.Any]:
    """Scenario-scoped context."""
    return {}


@given("a logging worker")
def given_logging_worker(context: dict[str, typ.Any]) -> None:
    """Define a worker that appends to a log."""
    log: list[str] = []

    async def logging_worker(*, log: list[str]) -> None:
        while True:
            log.append("ping")
            await asyncio.sleep(0)

    context["log"] = log
    context["worker"] = logging_worker


@given("a logging worker and a failing worker factory")
def given_worker_factory_failure(context: dict[str, typ.Any]) -> None:
    """Define a worker and a factory that raises a retained error."""
    log: list[str] = []
    failure_message = "worker factory failed"
    failure = ValueError(failure_message)

    async def logging_worker() -> None:
        """Append to a log until the controller cancels this worker."""
        while True:
            log.append("ping")
            await asyncio.sleep(0)

    def failing_factory() -> cabc.Coroutine[object, object, None]:
        """Raise the original factory error without creating a coroutine."""
        raise failure

    context["controller"] = WorkerController()
    context["failure"] = failure
    context["failing_factory"] = failing_factory
    context["log"] = log
    context["worker"] = logging_worker


@when("the worker controller starts and then stops it")
def when_run_worker(context: dict[str, typ.Any]) -> None:
    """Run the worker briefly under the controller."""
    controller = WorkerController()
    log = context["log"]
    worker = context["worker"]

    async def _run() -> None:
        await controller.start(worker, log=log)
        await asyncio.sleep(0.01)
        await controller.stop()

    asyncio.run(_run())


@when("both workers are started")
def when_both_workers_start(context: dict[str, typ.Any]) -> None:
    """Capture the startup error and any worker tasks left running."""
    controller = context["controller"]
    created_tasks: list[asyncio.Task[None]] = []

    async def _run() -> None:
        original_create_task = asyncio.create_task

        def capture_task(
            coroutine: cabc.Coroutine[object, object, None],
        ) -> asyncio.Task[None]:
            """Capture a task created during the failed startup."""
            task = original_create_task(coroutine)
            created_tasks.append(task)
            return task

        with mock.patch.object(asyncio, "create_task", capture_task):
            try:
                await controller.start(context["worker"], context["failing_factory"])
            except ValueError as error:
                context["startup_error"] = error
            else:
                msg = "The failing worker factory should abort startup"
                raise AssertionError(msg)

        context["created_tasks_after_failure"] = created_tasks

    asyncio.run(_run())


@then("startup propagates the original error and leaves no worker running")
def then_startup_rolled_back(context: dict[str, typ.Any]) -> None:
    """Verify the factory error is preserved and no worker task remains."""
    worker_tasks = context["created_tasks_after_failure"]
    assert context["startup_error"] is context["failure"], (
        "startup should propagate the original factory error"
    )
    assert len(worker_tasks) == 1, "startup should have scheduled one worker"
    assert all(task.done() and task.cancelled() for task in worker_tasks), (
        "failed startup should cancel and await each scheduled worker"
    )


@when("the logging worker is restarted")
def when_restart_logging_worker(context: dict[str, typ.Any]) -> None:
    """Start the valid worker again and let it run before stopping it."""
    controller = context["controller"]
    context["log"].clear()

    async def _run() -> None:
        await controller.start(context["worker"])
        await asyncio.sleep(0)
        await controller.stop()

    asyncio.run(_run())


@then("the log should contain at least one entry")
def then_log_not_empty(context: dict[str, typ.Any]) -> None:
    """Verify the worker executed."""
    assert context["log"], "the worker should have logged at least one entry"


@then("the logging worker runs after retry")
def then_logging_worker_runs_after_retry(context: dict[str, typ.Any]) -> None:
    """Verify the worker ran after the controller was restarted."""
    assert context["log"], "the retried worker should log at least one entry"
