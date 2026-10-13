"""Run an ASGI application on a real loopback WebSocket server for tests."""

from __future__ import annotations

import asyncio
import contextlib
import typing as typ

from ._common import MissingDependencyError

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    import uvicorn

    class _StartupServer(typ.Protocol):
        started: bool


_MISSING_UVICORN_MSG = (
    "serve_asgi_app requires the 'uvicorn' package. Install "
    "falcon-pachinko[testing] to enable this helper."
)


async def _wait_for_startup(
    server: _StartupServer,
    serve_task: asyncio.Task[None],
    timeout_seconds: float,
) -> None:
    """Wait for the listening socket and surface early server failure."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_seconds
    while not server.started:
        if serve_task.done():
            await serve_task
            msg = "ASGI server stopped before completing startup"
            raise RuntimeError(msg)
        remaining = deadline - loop.time()
        if remaining <= 0:
            msg = f"ASGI server did not start within {timeout_seconds:g} seconds"
            raise TimeoutError(msg)
        await asyncio.sleep(min(0.01, remaining))


def _bound_port(server: uvicorn.Server) -> int:
    """Return the first listener's bound TCP port."""
    for listener in server.servers:
        for listener_socket in listener.sockets or ():
            return int(listener_socket.getsockname()[1])
    msg = "ASGI server started without a listening socket"
    raise RuntimeError(msg)


async def _stop_server(
    server: uvicorn.Server,
    serve_task: asyncio.Task[None],
    timeout_seconds: float,
) -> None:
    """Request graceful shutdown, cancelling only after its deadline."""
    server.should_exit = True
    try:
        await asyncio.wait_for(asyncio.shield(serve_task), timeout=timeout_seconds)
    except TimeoutError as error:
        server.force_exit = True
        serve_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await serve_task
        msg = f"ASGI server did not shut down within {timeout_seconds:g} seconds"
        raise TimeoutError(msg) from error


async def _serve(server: uvicorn.Server) -> None:
    """Keep Uvicorn's startup ``SystemExit`` inside its task boundary."""
    try:
        await server.serve()
    except SystemExit as error:
        if server.started:
            msg = f"Uvicorn server exited with status {error.code}"
        else:
            msg = (
                f"ASGI lifespan startup failed (server exited with status {error.code})"
            )
        raise RuntimeError(msg) from error


@contextlib.asynccontextmanager
async def serve_asgi_app(
    app: cabc.Callable[..., cabc.Awaitable[None]],
    *,
    host: str = "127.0.0.1",
    startup_timeout: float = 10.0,
    shutdown_timeout: float = 10.0,
) -> cabc.AsyncIterator[str]:
    """Serve ``app`` on an ephemeral TCP port and yield its ``ws://`` URL.

    Uvicorn runs on the current event loop with ASGI lifespan enabled and its
    ``websockets`` protocol implementation. The context waits for startup
    before yielding, then requests graceful shutdown and waits for the app's
    lifespan shutdown handler before it returns.

    Parameters
    ----------
    app : Callable[..., Awaitable[None]]
        The ASGI application to serve.
    host : str, optional
        Interface on which to bind, by default ``"127.0.0.1"``.
    startup_timeout : float, optional
        Seconds to wait for ASGI startup, by default ``10.0``.
    shutdown_timeout : float, optional
        Seconds to wait for server and lifespan shutdown, by default ``10.0``.

    Yields
    ------
    str
        A WebSocket base URL such as ``"ws://127.0.0.1:43210"``.

    Raises
    ------
    MissingDependencyError
        If the optional Uvicorn dependency is not installed.
    ValueError
        If either timeout is not positive.
    """
    if startup_timeout <= 0 or shutdown_timeout <= 0:
        msg = "startup_timeout and shutdown_timeout must be positive"
        raise ValueError(msg)

    try:
        import uvicorn
    except ImportError as exc:  # pragma: no cover - tested without dependency
        raise MissingDependencyError(_MISSING_UVICORN_MSG) from exc

    class _SignalFreeServer(uvicorn.Server):
        @contextlib.contextmanager
        @typ.override
        def capture_signals(self) -> cabc.Iterator[None]:
            """Keep server shutdown local to the async context manager."""
            yield

    config = uvicorn.Config(
        app,
        host=host,
        port=0,
        lifespan="on",
        ws="websockets",
        log_level="warning",
        access_log=False,
    )
    server = _SignalFreeServer(config)
    serve_task = asyncio.create_task(_serve(server))
    try:
        await _wait_for_startup(server, serve_task, startup_timeout)
        port = _bound_port(server)
        url_host = f"[{host}]" if ":" in host and not host.startswith("[") else host
        yield f"ws://{url_host}:{port}"
    finally:
        await _stop_server(server, serve_task, shutdown_timeout)
