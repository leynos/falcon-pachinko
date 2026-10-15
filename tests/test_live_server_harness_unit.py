"""Failure-path tests for the live Uvicorn acceptance harness."""

from __future__ import annotations

import asyncio
import signal
import typing as typ
from contextlib import suppress

import pytest
import uvicorn

from tests.behaviour import _live_server, _router_live_server

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


@pytest.mark.asyncio
async def test_readiness_wait_preserves_finished_serving_failure() -> None:
    """A listener that fails before readiness must expose its original failure."""
    failure = RuntimeError("listener failed")

    async def fail_serving() -> None:
        await asyncio.sleep(0)
        raise failure

    task = asyncio.create_task(fail_serving())
    try:
        await asyncio.sleep(0)
        with pytest.raises(RuntimeError, match="listener failed") as raised:
            await asyncio.wait_for(
                _live_server._wait_until_started(
                    typ.cast("Server", _UnreadyServer()), task
                ),
                timeout=1,
            )
        assert raised.value is failure, "readiness must retain the serving exception"
    finally:
        await asyncio.gather(task, return_exceptions=True)


def test_live_server_preserves_test_runner_signal_handlers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The Uvicorn server must not install or restore process signal handlers."""
    calls: list[int] = []

    def record_signal(signum: int, handler: object) -> int:
        del handler
        calls.append(signum)
        return signal.SIG_DFL

    monkeypatch.setattr(signal, "signal", record_signal)
    server = _router_live_server._SignalFreeServer(
        uvicorn.Config(_live_server.create_live_application().app, lifespan="off")
    )
    with server.capture_signals():
        assert not calls, "server startup must leave pytest signal ownership alone"
    assert not calls, "server exit must leave pytest signal ownership alone"


@pytest.mark.asyncio
async def test_readiness_rejects_listener_that_exits_before_starting() -> None:
    """A clean early serving exit must not wait for the readiness timeout."""
    task = asyncio.create_task(asyncio.sleep(0))
    try:
        await task
        with pytest.raises(RuntimeError, match="stopped before"):
            await asyncio.wait_for(
                _live_server._wait_until_started(
                    typ.cast("Server", _UnreadyServer()), task
                ),
                timeout=1,
            )
    finally:
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_live_server_retains_supplied_application_metadata() -> None:
    """The application factory is called once and its bundle stays observable."""
    application = _live_server.create_live_application()
    calls: list[_live_server.LiveApplication] = []

    def make_application() -> _live_server.LiveApplication:
        calls.append(application)
        return application

    server = await asyncio.wait_for(
        _live_server.LiveServer.start(
            asyncio.get_running_loop(), application_factory=make_application
        ),
        timeout=10,
    )
    try:
        assert server.application is application, "the factory bundle must be retained"
        assert calls == [application], "application construction must happen once"
        assert server.server.config.app is application.app, (
            "Uvicorn must serve that app"
        )
    finally:
        await asyncio.wait_for(server.stop(), timeout=10)
