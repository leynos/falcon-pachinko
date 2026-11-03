"""Run the lifecycle behaviour application on a real Uvicorn server."""

from __future__ import annotations

import asyncio
import collections.abc as cabc
import dataclasses as dc
import typing as typ
from contextlib import suppress

import uvicorn

from tests.behaviour._live_lifecycle_app import create_live_application

if typ.TYPE_CHECKING:
    from tests.behaviour._live_lifecycle_app import LiveApplication

type _LoopExceptionHandler = cabc.Callable[
    [asyncio.AbstractEventLoop, dict[str, typ.Any]], None
]


@dc.dataclass(slots=True)
class LiveServer:
    """Run Uvicorn on a caller-owned event loop and capture loop errors."""

    application: LiveApplication
    server: uvicorn.Server
    serve_task: asyncio.Task[None]
    loop: asyncio.AbstractEventLoop
    port: int
    loop_errors: list[dict[str, object]]
    _previous_exception_handler: _LoopExceptionHandler | None = dc.field(repr=False)
    _stopped: bool = False

    @classmethod
    async def start(
        cls,
        loop: asyncio.AbstractEventLoop,
        *,
        server_factory: cabc.Callable[
            [uvicorn.Config], uvicorn.Server
        ] = uvicorn.Server,
        wait_ready: cabc.Callable[
            [cabc.Coroutine[object, object, None]], cabc.Awaitable[None]
        ]
        | None = None,
    ) -> LiveServer:
        """Start one ephemeral Uvicorn listener and wait for readiness."""
        application = create_live_application()
        server = server_factory(_server_config(application))
        loop_errors: list[dict[str, object]] = []
        previous_handler = typ.cast(
            "_LoopExceptionHandler | None", loop.get_exception_handler()
        )
        loop.set_exception_handler(_exception_recorder(loop_errors))
        serve_task = asyncio.create_task(server.serve())
        try:
            await _wait_for_startup(server, serve_task, wait_ready)
            port = _listening_port(server)
        except (Exception, asyncio.CancelledError):
            await _cleanup_failed_start(loop, previous_handler, serve_task)
            raise
        return cls(
            application,
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
        try:
            await asyncio.wait_for(asyncio.shield(self.serve_task), timeout=5)
        except TimeoutError:
            self.serve_task.cancel()
            with suppress(asyncio.CancelledError):
                await self.serve_task
            raise
        finally:
            try:
                # Uvicorn's graceful timeout cancels ASGI tasks without joining
                # them. Keep the loop alive until their shielded cleanup ends.
                session_tasks = tuple(self.server.server_state.tasks)
                if session_tasks:
                    await asyncio.wait_for(
                        asyncio.gather(*session_tasks, return_exceptions=True),
                        timeout=5,
                    )
            finally:
                self.loop.set_exception_handler(self._previous_exception_handler)


def _server_config(application: LiveApplication) -> uvicorn.Config:
    """Build the loopback-only configuration used by live tests."""
    return uvicorn.Config(
        application.app,
        host="127.0.0.1",
        port=0,
        ws="websockets",
        lifespan="off",
        timeout_graceful_shutdown=1,
        access_log=False,
        log_level="critical",
    )


def _exception_recorder(
    loop_errors: list[dict[str, object]],
) -> _LoopExceptionHandler:
    """Return a handler that records unexpected loop errors during a test."""

    def record_loop_error(
        _loop: asyncio.AbstractEventLoop, context: dict[str, typ.Any]
    ) -> None:
        """Retain unexpected ASGI task failures for live test assertions."""
        loop_errors.append(context)

    return record_loop_error


async def _wait_for_startup(
    server: uvicorn.Server,
    serve_task: asyncio.Task[None],
    wait_ready: cabc.Callable[
        [cabc.Coroutine[object, object, None]], cabc.Awaitable[None]
    ]
    | None,
) -> None:
    """Wait for server readiness or an injected test synchronization point."""
    wait_until_started = _wait_until_started(server, serve_task)
    if wait_ready is None:
        await asyncio.wait_for(wait_until_started, timeout=5)
    else:
        await wait_ready(wait_until_started)


async def _wait_until_started(
    server: uvicorn.Server, serve_task: asyncio.Task[None]
) -> None:
    """Poll the ready flag while surfacing an early serving-task failure."""
    while not server.started:
        if serve_task.done():
            serve_task.result()
        await asyncio.sleep(0.01)


def _listening_port(server: uvicorn.Server) -> int:
    """Return the first bound port, or fail if startup exposed no listener."""
    sockets = server.servers
    if sockets and sockets[0].sockets:
        return typ.cast("int", sockets[0].sockets[0].getsockname()[1])
    msg = "Uvicorn reported started without a listening socket"
    raise RuntimeError(msg)


async def _cleanup_failed_start(
    loop: asyncio.AbstractEventLoop,
    previous_handler: _LoopExceptionHandler | None,
    serve_task: asyncio.Task[None],
) -> None:
    """Stop a failed start and restore the event loop's prior error handler."""
    serve_task.cancel()
    try:
        await asyncio.gather(serve_task, return_exceptions=True)
    finally:
        loop.set_exception_handler(previous_handler)
