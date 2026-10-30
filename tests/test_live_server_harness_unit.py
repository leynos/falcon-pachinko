"""Failure-path tests for the live Uvicorn acceptance harness."""

from __future__ import annotations

import asyncio
import signal
import typing as typ
from contextlib import suppress
from types import SimpleNamespace

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


class _StartedWithoutSocketServer:
    """Announce readiness without a listener and expose shutdown ordering."""

    def __init__(
        self,
        *,
        fail_during_shutdown: bool = False,
        wait_during_shutdown: bool = False,
    ) -> None:
        self.started = False
        self.servers: list[object] = []
        self.stop_requested = asyncio.Event()
        self.stopped = asyncio.Event()
        self.release_stop = asyncio.Event()
        self.fail_during_shutdown = fail_during_shutdown
        self.wait_during_shutdown = wait_during_shutdown
        self.task: asyncio.Task[None] | None = None

    async def serve(self) -> None:
        """Stay alive after readiness until startup cleanup cancels the task."""
        self.task = asyncio.current_task()
        self.started = True
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError as cancellation_error:
            self.stop_requested.set()
            if self.wait_during_shutdown:
                try:
                    await self.release_stop.wait()
                except asyncio.CancelledError:
                    self.stopped.set()
                    raise
            if self.fail_during_shutdown:
                msg = "serve task failed while stopping"
                raise RuntimeError(msg) from cancellation_error
            raise
        finally:
            self.stopped.set()


def test_loop_error_assertion_accepts_an_empty_capture() -> None:
    """A clean live server leaves no unacknowledged loop failures."""
    _live_server._assert_no_loop_errors([])


def test_loop_error_assertion_reports_unacknowledged_failures() -> None:
    """A captured loop failure cannot disappear during fixture teardown."""
    failure = RuntimeError("cleanup failure")

    with pytest.raises(AssertionError, match="cleanup failure"):
        _live_server._assert_no_loop_errors([
            {"exception": failure},
            {"message": "secondary failure"},
        ])


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
async def test_missing_listener_preserves_startup_error_and_joins_server() -> None:
    """A serving-task failure during cancellation cannot replace startup error."""
    server = _StartedWithoutSocketServer(fail_during_shutdown=True)
    loop = asyncio.get_running_loop()
    previous_handler = loop.get_exception_handler()

    with pytest.raises(RuntimeError, match="without a listening socket"):
        await _live_server.LiveServer.start(
            loop,
            server_factory=lambda _config: typ.cast("Server", server),
        )

    assert server.stopped.is_set(), "startup must join the serving task"
    assert loop.get_exception_handler() is previous_handler, (
        "startup failure must restore the previous loop exception handler"
    )


@pytest.mark.asyncio
async def test_missing_listener_does_not_swallow_caller_cancellation() -> None:
    """Cancellation while joining a listener must remain visible to the caller."""
    server = _StartedWithoutSocketServer(wait_during_shutdown=True)
    loop = asyncio.get_running_loop()
    previous_handler = loop.get_exception_handler()
    startup = asyncio.create_task(
        _live_server.LiveServer.start(
            loop,
            server_factory=lambda _config: typ.cast("Server", server),
        )
    )
    try:
        await asyncio.wait_for(server.stop_requested.wait(), timeout=1)
        startup.cancel("caller stopped startup")
        with pytest.raises(asyncio.CancelledError, match="caller stopped startup"):
            await asyncio.wait_for(startup, timeout=1)
        assert server.stopped.is_set(), (
            "startup cancellation must join the serving task"
        )
        assert loop.get_exception_handler() is previous_handler, (
            "caller cancellation must restore the previous loop handler"
        )
    finally:
        server.release_stop.set()
        if not startup.done():
            startup.cancel()
        if server.task is not None:
            server.task.cancel()
            await asyncio.gather(startup, server.task, return_exceptions=True)
        else:
            await asyncio.gather(startup, return_exceptions=True)


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
async def test_stop_preserves_serve_failure_when_session_join_times_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cleanup join timeout must not replace the original serve failure."""
    failure = RuntimeError("serve loop failed")

    async def fail_serve() -> None:
        await asyncio.sleep(0)
        raise failure

    serve_task = asyncio.create_task(fail_serve())
    await asyncio.gather(serve_task, return_exceptions=True)
    pending_session = asyncio.create_task(asyncio.Event().wait())
    fake_server = typ.cast(
        "Server",
        SimpleNamespace(
            should_exit=False,
            server_state=SimpleNamespace(tasks={pending_session}),
        ),
    )
    monkeypatch.setattr(_live_server, "_SESSION_TASK_JOIN_TIMEOUT", 0.01)
    loop = asyncio.get_running_loop()
    previous_handler = typ.cast(
        "_live_server._LoopExceptionHandler | None", loop.get_exception_handler()
    )
    transport = _live_server.LiveServerTransport(
        server=fake_server,
        serve_task=serve_task,
        loop=loop,
        port=0,
        loop_errors=[],
        _previous_exception_handler=previous_handler,
    )
    try:
        with pytest.raises(RuntimeError, match="serve loop failed") as raised:
            await asyncio.wait_for(transport.stop(), timeout=1)
        assert raised.value is failure, "stop must preserve the original server error"
        assert pending_session.cancelled(), "the timed-out join must cancel its tasks"
        assert loop.get_exception_handler() is previous_handler, (
            "stop must restore the original loop exception handler"
        )
    finally:
        if not pending_session.done():
            pending_session.cancel()
        await asyncio.gather(pending_session, return_exceptions=True)


@pytest.mark.asyncio
async def test_stop_reports_session_join_timeout_inside_caller_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An outer handled error must not hide a failed session-task join."""
    serve_task = asyncio.create_task(asyncio.sleep(0))
    await serve_task
    pending_session = asyncio.create_task(asyncio.Event().wait())
    fake_server = typ.cast(
        "Server",
        SimpleNamespace(
            should_exit=False,
            server_state=SimpleNamespace(tasks={pending_session}),
        ),
    )
    monkeypatch.setattr(_live_server, "_SESSION_TASK_JOIN_TIMEOUT", 0.01)
    loop = asyncio.get_running_loop()
    previous_handler = typ.cast(
        "_live_server._LoopExceptionHandler | None", loop.get_exception_handler()
    )
    transport = _live_server.LiveServerTransport(
        server=fake_server,
        serve_task=serve_task,
        loop=loop,
        port=0,
        loop_errors=[],
        _previous_exception_handler=previous_handler,
    )

    caller_failure = ValueError()

    def raise_caller_failure() -> None:
        raise caller_failure

    try:
        try:
            raise_caller_failure()
        except ValueError:
            with pytest.raises(TimeoutError):
                await transport.stop()
        assert pending_session.cancelled(), "a timed-out join must cancel its tasks"
        assert loop.get_exception_handler() is previous_handler, (
            "stop must restore the original loop exception handler"
        )
    finally:
        if not pending_session.done():
            pending_session.cancel()
        await asyncio.gather(pending_session, return_exceptions=True)


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
