"""Random status example using lifespan-managed workers.

Run the accompanying ``client.py`` script to interact with the server.
"""
# /// script
# dependencies = [
#     "falcon",
#     "falcon-pachinko",
#     "msgspec",
#     "aiosqlite",
#     "uvicorn",
# ]
# ///

from __future__ import annotations

import asyncio
import contextlib as cl
import secrets
import typing as t

import aiosqlite
import falcon.asgi

from falcon_pachinko import (
    WebSocketConnectionManager,
    WebSocketLike,
    WebSocketResource,
    WorkerController,
    handles_message,
    install,
    worker,
)

if t.TYPE_CHECKING:
    import collections.abc as cabc
    import contextlib as cl_typing


async def _setup_db() -> aiosqlite.Connection:
    conn = await aiosqlite.connect(":memory:")
    await conn.execute("CREATE TABLE status(value TEXT)")
    await conn.execute("INSERT INTO status(value) VALUES('ready')")
    await conn.commit()
    return conn


# Lazily initialized during startup to avoid import-time event loop work
DB: aiosqlite.Connection | None = None


class StatusPayload(t.TypedDict):
    """Type definition for status message payload."""

    text: str


@worker
async def random_worker(*, conn_mgr: WebSocketConnectionManager) -> None:
    """Periodically broadcast random numbers to all connections."""
    try:
        while True:
            await asyncio.sleep(5)
            number = secrets.randbelow(65536)
            for ws in list(conn_mgr.connections.values()):
                with cl.suppress(ConnectionError, OSError):
                    await ws.send_media({"type": "random", "payload": str(number)})
    except asyncio.CancelledError:
        pass


class StatusResource(WebSocketResource):
    """WebSocket resource for handling status updates."""

    def __init__(self, conn_mgr: WebSocketConnectionManager) -> None:
        self._conn_mgr = conn_mgr
        self._conn_id: str | None = None

    async def on_connect(
        self, req: falcon.Request, ws: WebSocketLike, **_: object
    ) -> bool:
        """Accept the connection and register it with the manager."""
        await ws.accept()
        conn_id = secrets.token_hex(16)
        self._conn_mgr.connections[conn_id] = ws
        self._conn_id = conn_id
        return True

    async def on_disconnect(self, ws: WebSocketLike, close_code: int) -> None:
        """Unregister the connection on disconnect."""
        if self._conn_id:
            self._conn_mgr.connections.pop(self._conn_id, None)

    @handles_message("status")
    async def update_status(self, ws: WebSocketLike, payload: StatusPayload) -> None:
        """Update the stored status value and acknowledge."""
        text = payload["text"]
        conn = DB
        if conn is None:
            msg = "DB connection not initialized"
            raise RuntimeError(msg)
        await conn.execute("UPDATE status SET value=?", (text,))
        await conn.commit()
        await ws.send_media({"type": "ack", "payload": text})


class StatusEndpoint:
    """HTTP endpoint for retrieving the current status value."""

    async def on_get(self, req: falcon.Request, resp: falcon.Response) -> None:
        """Return the current status value as JSON."""
        conn = DB
        if conn is None:
            msg = "DB connection not initialized"
            raise RuntimeError(msg)
        async with conn.execute("SELECT value FROM status") as cursor:
            row = await cursor.fetchone()
        resp.media = {"status": row[0] if row else None}


class LifespanApp(falcon.asgi.App):
    """Falcon ASGI App with a minimal lifespan decorator."""

    def __init__(self) -> None:
        super().__init__()
        self._lifespan_handler: (
            cabc.Callable[
                [falcon.asgi.App], cl_typing.AbstractAsyncContextManager[None]
            ]
            | None
        ) = None

    def lifespan(
        self, fn: cabc.Callable[[LifespanApp], cabc.AsyncIterator[None]]
    ) -> cabc.Callable[[LifespanApp], cl_typing.AbstractAsyncContextManager[None]]:  # type: ignore[override]
        """Register a lifespan context manager."""
        manager = cl.asynccontextmanager(fn)
        self._lifespan_handler = manager
        return manager

    def lifespan_context(self) -> cl_typing.AbstractAsyncContextManager[None]:
        """Return the registered lifespan context manager."""
        if self._lifespan_handler is None:
            msg = "lifespan handler not set"
            raise RuntimeError(msg)
        return self._lifespan_handler(self)


def create_app() -> falcon.asgi.App:
    """Create and configure the Falcon ASGI application."""
    app = LifespanApp()
    install(app)
    conn_mgr: WebSocketConnectionManager = app.ws_connection_manager  # type: ignore[attr-defined]

    controller = WorkerController()

    @app.lifespan
    async def lifespan(app_instance: LifespanApp) -> t.AsyncIterator[None]:
        # Lazily initialize the DB on startup
        global DB
        if DB is None:
            DB = await _setup_db()

        await controller.start(random_worker, conn_mgr=conn_mgr)
        try:
            yield
        finally:
            await controller.stop()
            # Best-effort close; swallow errors to not mask shutdown issues
            with cl.suppress(Exception):
                if DB is not None:
                    await DB.close()
                    DB = None

    app.add_websocket_route("/ws", lambda: StatusResource(conn_mgr))  # type: ignore[attr-defined]
    app.add_route("/status", StatusEndpoint())
    return app


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(create_app(), host="127.0.0.1", port=8000)
