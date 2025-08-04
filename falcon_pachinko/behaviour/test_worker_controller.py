"""Behavioural tests for the WorkerController lifecycle."""

from __future__ import annotations

import asyncio
import typing

import pytest
from pytest_bdd import given, scenario, then, when

from falcon_pachinko.workers import WorkerController


@scenario("features/worker_controller.feature", "Run a background worker")
def test_run_worker() -> None:
    """Scenario: Run a background worker."""


@pytest.fixture
def context() -> dict[str, typing.Any]:
    """Scenario-scoped context."""
    return {}


@given("a logging worker")
def given_logging_worker(context: dict[str, typing.Any]) -> None:
    """Define a worker that appends to a log."""
    log: list[str] = []

    async def logging_worker(*, log: list[str]) -> None:
        while True:
            log.append("ping")
            await asyncio.sleep(0)

    context["log"] = log
    context["worker"] = logging_worker


@when("the worker controller starts and then stops it")
def when_run_worker(context: dict[str, typing.Any]) -> None:
    """Run the worker briefly under the controller."""
    controller = WorkerController()
    log = context["log"]
    worker = context["worker"]

    async def _run() -> None:
        await controller.start(worker, log=log)
        await asyncio.sleep(0.01)
        await controller.stop()

    asyncio.run(_run())


@then("the log should contain at least one entry")
def then_log_not_empty(context: dict[str, typing.Any]) -> None:
    """Verify the worker executed."""
    assert context["log"]
