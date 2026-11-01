"""Real Uvicorn harness for production-router WebSocket lifecycle scenarios."""

from __future__ import annotations

import asyncio
import collections.abc as cabc
import dataclasses as dc
import typing as typ
from contextlib import contextmanager, suppress

import uvicorn

if typ.TYPE_CHECKING:
    import falcon.asgi

from tests.behaviour._live_lifecycle_app import LiveApplication, create_live_application

type _LoopExceptionHandler = cabc.Callable[
    [asyncio.AbstractEventLoop, dict[str, typ.Any]], None
]
_SESSION_TASK_JOIN_TIMEOUT = 5


def _assert_no_loop_errors(loop_errors: list[dict[str, object]]) -> None:
    """Fail when a scenario leaves a server-side error unacknowledged."""
    unexpected = [
        error.get("exception") or error.get("message") for error in loop_errors
    ]
    assert not unexpected, f"live server reported loop errors: {unexpected!r}"


class SignalFreeServer(uvicorn.Server):
    """Leave process signal ownership with the test runner."""

    @contextmanager
    @typ.override
    def capture_signals(self) -> cabc.Generator[None, None, None]:
        """Serve without installing or restoring process signal handlers."""
        yield


@dc.dataclass(slots=True)
class LiveServerTransport:
    """Run Uvicorn on a caller-owned event loop and capture loop errors."""

    server: uvicorn.Server
    serve_task: asyncio.Task[None]
    loop: asyncio.AbstractEventLoop
    port: int
    loop_errors: list[dict[str, object]]
    _previous_exception_handler: _LoopExceptionHandler | None = dc.field(repr=False)
    _stopped: bool = dc.field(default=False, init=False)

    @classmethod
    async def launch(
        cls,
        loop: asyncio.AbstractEventLoop,
        app: falcon.asgi.App[falcon.asgi.Request, falcon.asgi.Response],
        *,
        server_factory: cabc.Callable[
            [uvicorn.Config], uvicorn.Server
        ] = SignalFreeServer,
        wait_ready: cabc.Callable[
            [cabc.Coroutine[object, object, None]], cabc.Awaitable[None]
        ]
        | None = None,
    ) -> LiveServerTransport:
        """Start one ephemeral Uvicorn listener and wait for readiness."""
        config = uvicorn.Config(
            app,
            host="127.0.0.1",
            port=0,
            ws="websockets",
            lifespan="off",
            timeout_graceful_shutdown=1,
            access_log=False,
            log_level="critical",
        )
        server = server_factory(config)
        loop_errors: list[dict[str, object]] = []
        previous_handler = typ.cast(
            "_LoopExceptionHandler | None", loop.get_exception_handler()
        )

        def record_loop_error(
            _loop: asyncio.AbstractEventLoop, context: dict[str, typ.Any]
        ) -> None:
            loop_errors.append(context)

        loop.set_exception_handler(record_loop_error)
        serve_task = asyncio.create_task(server.serve())

        try:
            if wait_ready is None:
                await asyncio.wait_for(
                    _wait_until_started(server, serve_task), timeout=5
                )
            else:
                await wait_ready(_wait_until_started(server, serve_task))
        except (Exception, asyncio.CancelledError):
            serve_task.cancel()
            try:
                await asyncio.gather(serve_task, return_exceptions=True)
            finally:
                loop.set_exception_handler(previous_handler)
            raise

        sockets = server.servers
        if not sockets or not sockets[0].sockets:
            serve_task.cancel()
            try:
                await asyncio.gather(serve_task, return_exceptions=True)
            finally:
                loop.set_exception_handler(previous_handler)
            msg = "Uvicorn reported started without a listening socket"
            raise RuntimeError(msg)
        port = sockets[0].sockets[0].getsockname()[1]
        return cls(
            server,
            serve_task,
            loop,
            port,
            loop_errors,
            previous_handler,
        )

    @property
    def base_url(self) -> str:
        """Insecure loopback URL used by the local test client."""
        return f"ws://127.0.0.1:{self.port}"

    async def stop(self) -> None:
        """Ask Uvicorn to shut down and join its serving task."""
        if self._stopped:
            return
        self._stopped = True
        self.server.should_exit = True
        primary_error: BaseException | None = None
        try:
            try:
                await self._stop_serving()
            except BaseException as exc:
                primary_error = exc
                raise
        finally:
            try:
                await self._join_sessions(preserve_primary=primary_error is not None)
            finally:
                self.loop.set_exception_handler(self._previous_exception_handler)

    async def _stop_serving(self) -> None:
        try:
            await asyncio.wait_for(asyncio.shield(self.serve_task), timeout=5)
        except TimeoutError:
            self.serve_task.cancel()
            with suppress(asyncio.CancelledError):
                await self.serve_task
            raise

    async def _join_sessions(self, *, preserve_primary: bool) -> None:
        # Uvicorn cancels ASGI tasks without joining them after its graceful
        # timeout, so keep the loop alive until their shielded cleanup ends.
        session_tasks = tuple(self.server.server_state.tasks)
        if not session_tasks:
            return
        joined = asyncio.gather(*session_tasks, return_exceptions=True)
        try:
            await asyncio.wait_for(joined, timeout=_SESSION_TASK_JOIN_TIMEOUT)
        except TimeoutError:
            if not preserve_primary:
                raise


@dc.dataclass(slots=True)
class LiveServer(LiveServerTransport):
    """Keep lifecycle application observations alongside the shared transport."""

    application: LiveApplication

    @classmethod
    async def start(
        cls,
        loop: asyncio.AbstractEventLoop,
        *,
        application_factory: cabc.Callable[
            [], LiveApplication
        ] = create_live_application,
        server_factory: cabc.Callable[
            [uvicorn.Config], uvicorn.Server
        ] = SignalFreeServer,
        wait_ready: cabc.Callable[
            [cabc.Coroutine[object, object, None]], cabc.Awaitable[None]
        ]
        | None = None,
    ) -> LiveServer:
        """Build the recording application and start its owned transport."""
        application = application_factory()
        transport = await LiveServerTransport.launch(
            loop, application.app, server_factory=server_factory, wait_ready=wait_ready
        )
        return cls(
            application=application,
            server=transport.server,
            serve_task=transport.serve_task,
            loop=transport.loop,
            port=transport.port,
            loop_errors=transport.loop_errors,
            _previous_exception_handler=transport._previous_exception_handler,
        )


async def _wait_until_started(
    server: uvicorn.Server, serve_task: asyncio.Task[None]
) -> None:
    """Wait for the listener while preserving an early serving failure."""
    while not server.started:
        if serve_task.done():
            serve_task.result()
            msg = "Uvicorn stopped before its live server started"
            raise RuntimeError(msg)
        await asyncio.sleep(0.01)
