"""Lazy Uvicorn adapter for the live WebSocket test server."""

from __future__ import annotations

import asyncio
import importlib
import typing as typ
from contextlib import contextmanager, suppress

from ._common import MissingDependencyError

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    import socket
    from types import ModuleType

    from .live import ASGIApplication

_MISSING_UVICORN_MSG = (
    "LiveWebSocketServer requires the 'uvicorn' package. Install "
    "falcon-pachinko[testing] to enable the live test server."
)


def _load_uvicorn() -> ModuleType:
    """Import Uvicorn only when its adapter is asked to start."""
    try:
        return importlib.import_module("uvicorn")
    except ImportError as exc:
        raise MissingDependencyError(_MISSING_UVICORN_MSG) from exc


class _UvicornAdapter:
    """Serve one ASGI application on a socket bound by the harness."""

    def __init__(self, *, shutdown_timeout: float) -> None:
        loop = asyncio.get_running_loop()
        self._start_request: asyncio.Future[tuple[object, socket.socket]] = (
            loop.create_future()
        )
        self._ready = asyncio.Event()
        self._server: typ.Any | None = None
        self._shutdown_timeout = shutdown_timeout
        self._task = asyncio.create_task(self._serve_after_start())

    @property
    def task(self) -> asyncio.Task[None]:
        """The task that owns the Uvicorn serve loop."""
        return self._task

    async def start(self, app: ASGIApplication, sock: socket.socket) -> None:
        """Start Uvicorn and wait for completed lifespan and listener setup."""
        if self._start_request.done():
            msg = "a Uvicorn adapter can only be started once"
            raise RuntimeError(msg)
        self._start_request.set_result((app, sock))
        ready_waiter = asyncio.create_task(self._ready.wait())
        try:
            await self._wait_for_readiness(ready_waiter)
        finally:
            if not ready_waiter.done():
                ready_waiter.cancel()
            with suppress(asyncio.CancelledError):
                await ready_waiter

    async def _wait_for_readiness(self, ready_waiter: asyncio.Task[bool]) -> None:
        """Fail if serving ends before Uvicorn completes startup."""
        done, _pending = await asyncio.wait(
            {ready_waiter, self._task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if self._task in done:
            self._raise_if_server_stopped("before startup readiness")
        await ready_waiter
        if self._task.done():
            self._raise_if_server_stopped("during startup")

    def _raise_if_server_stopped(self, stage: str) -> typ.NoReturn:
        """Raise the serve-task error or describe its early clean exit."""
        if self._task.cancelled():
            msg = f"Uvicorn stopped {stage}"
            raise RuntimeError(msg)
        error = self._task.exception()
        if error is not None:
            raise error
        msg = f"Uvicorn stopped {stage}"
        raise RuntimeError(msg)

    async def stop(self) -> None:
        """Request graceful shutdown and join the Uvicorn serve task."""
        server = self._server
        if server is None:
            self._task.cancel()
        else:
            server.should_exit = True
        try:
            await self._task
        except asyncio.CancelledError:
            if not self._task.cancelled():
                raise

    async def _serve_after_start(self) -> None:
        """Create the Uvicorn server after the harness supplies its socket."""
        app, sock = await self._start_request
        uvicorn = typ.cast("typ.Any", _load_uvicorn())
        config = uvicorn.Config(
            app,
            host="127.0.0.1",
            port=0,
            lifespan="on",
            log_config=None,
            log_level="critical",
            interface="asgi3",
            timeout_graceful_shutdown=self._shutdown_timeout,
        )
        ready = self._ready

        class _ReadinessServer(uvicorn.Server):
            """Signal readiness only after Uvicorn installs the listener."""

            @typ.override
            async def startup(self, sockets: list[socket.socket] | None = None) -> None:
                await super().startup(sockets=sockets)
                if not self.should_exit:
                    ready.set()

            @typ.override
            @contextmanager
            def capture_signals(self) -> cabc.Iterator[None]:
                """Leave signal ownership with the test process."""
                yield

        server = _ReadinessServer(config)
        self._server = server
        try:
            await server.serve(sockets=[sock])
        except SystemExit as exc:
            msg = f"Uvicorn exited with status {exc.code!r}"
            raise RuntimeError(msg) from exc
        if not self._ready.is_set():
            msg = "Uvicorn exited before lifespan startup completed"
            raise RuntimeError(msg)
