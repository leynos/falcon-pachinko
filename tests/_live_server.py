"""Bounded live Uvicorn servers on independently pre-bound loopback sockets."""

from __future__ import annotations

import asyncio
import socket
import typing as typ
from contextlib import asynccontextmanager

import uvicorn

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    import falcon.asgi


async def _wait_started(server: uvicorn.Server, task: asyncio.Task[None]) -> None:
    """Reject an exited server immediately while polling Uvicorn's readiness flag."""
    # Uvicorn exposes readiness as a flag rather than an awaitable event.
    while not server.started:
        if task.done():
            await task
            msg = "live test server exited before startup"
            raise RuntimeError(msg)
        await asyncio.sleep(0.01)
    if task.done():
        await task
        msg = "live test server exited before startup"
        raise RuntimeError(msg)


async def _stop_server(server: uvicorn.Server, task: asyncio.Task[None]) -> None:
    """Signal graceful exit and await this helper's server task."""
    server.should_exit = True
    await asyncio.shield(task)


@asynccontextmanager
async def live_server(
    app: falcon.asgi.App,
    *,
    startup_timeout: float = 5.0,
    shutdown_timeout: float = 5.0,
) -> cabc.AsyncIterator[str]:
    """Yield a real WebSocket URL, cancelling only this helper's stalled server."""
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        listener.settimeout(0.0)
        port = listener.getsockname()[1]
        server = uvicorn.Server(
            uvicorn.Config(app, lifespan="off", ws="websockets", log_level="warning")
        )
        task = asyncio.create_task(server.serve(sockets=[listener]))
        try:
            async with asyncio.timeout(startup_timeout):
                await _wait_started(server, task)
            yield f"ws://127.0.0.1:{port}"
        finally:
            try:
                async with asyncio.timeout(shutdown_timeout):
                    await _stop_server(server, task)
            except TimeoutError:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
    finally:
        listener.close()
