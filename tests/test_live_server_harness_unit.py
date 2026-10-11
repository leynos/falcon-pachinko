"""Failure-path tests for the live Uvicorn acceptance harness."""

from __future__ import annotations

import asyncio
import typing as typ
from contextlib import suppress

import pytest

from tests.behaviour import _live_server

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from uvicorn import Server


class _UnreadyServer:
    """Keep an owned serving task alive without announcing readiness."""

    def __init__(self) -> None:
        self.started = False
        self.serving = asyncio.Event()
        self.stopped = asyncio.Event()
        self.task: asyncio.Task[None] | None = None

    async def serve(self) -> None:
        """Remain pending until the startup failure path cancels this task."""
        self.task = asyncio.current_task()
        self.serving.set()
        try:
            await asyncio.Event().wait()
        finally:
            self.stopped.set()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "startup_error",
    [
        pytest.param(TimeoutError("startup failed"), id="timeout"),
        pytest.param(asyncio.CancelledError("startup cancelled"), id="cancellation"),
    ],
)
async def test_startup_failure_joins_owned_serving_task(
    startup_error: TimeoutError | asyncio.CancelledError,
) -> None:
    """Failed readiness preserves the error and leaves no serving task alive."""
    server = _UnreadyServer()

    async def fail_readiness(
        pending: cabc.Coroutine[object, object, None],
    ) -> None:
        await asyncio.wait_for(server.serving.wait(), timeout=1)
        pending.close()
        raise startup_error

    loop = asyncio.get_running_loop()
    previous_handler = loop.get_exception_handler()
    try:
        with pytest.raises(type(startup_error), match="startup") as raised:
            await _live_server.LiveServer.start(
                loop,
                server_factory=lambda _config: typ.cast("Server", server),
                wait_ready=fail_readiness,
            )
        assert raised.value is startup_error, "startup must preserve the original error"
        assert server.stopped.is_set(), "startup failure must join its serving task"
        assert loop.get_exception_handler() is previous_handler, (
            "startup failure must restore the original loop exception handler"
        )
    finally:
        if server.task is not None:
            server.task.cancel()
            with suppress(asyncio.CancelledError):
                await server.task
