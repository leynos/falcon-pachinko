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
import typing as typ

import aiosqlite
import falcon.asgi as falcon_asgi

from falcon_pachinko import (
    WebSocketConnectionManager,
    WebSocketLike,
    WebSocketResource,
    WorkerController,
    handles_message,
    install,
    worker,
)

try:
    from tests.behaviour._lifespan import LifespanApp  # type: ignore[import-not-found]
except Exception:  # noqa: BLE001
    import contextlib as cl
    import typing as typ

    class LifespanApp(falcon_asgi.App):
        """Falcon ASGI App with a minimal lifespan decorator (local fallback)."""

        def __init__(self) -> None:
            super().__init__()
            self._lifespan_handler: (
                typ.Callable[[LifespanApp], cl.AbstractAsyncContextManager[None]] | None
            ) = None

        def lifespan(
            self, fn: typ.Callable[[typ.Any], typ.AsyncIterator[None]]
        ) -> typ.Callable[[typ.Any], cl.AbstractAsyncContextManager[None]]:
            """Register a lifespan context manager."""
            manager = cl.asynccontextmanager(fn)
            self._lifespan_handler = manager
            return manager

        def lifespan_context(self) -> cl.AbstractAsyncContextManager[None]:
            """Return the registered lifespan context manager."""
            if self._lifespan_handler is None:
                msg = "lifespan handler not set"
                raise RuntimeError(msg)
            return self._lifespan_handler(self)


if typ.TYPE_CHECKING:
    import falcon


async def _setup_db() -> aiosqlite.Connection:
    conn = await aiosqlite.connect(":memory:")
    await conn.execute("CREATE TABLE status(value TEXT)")
    await conn.execute("INSERT INTO status(value) VALUES('ready')")
    await conn.commit()
    return conn


# Lazily initialized during startup to avoid import-time event loop work
DB: aiosqlite.Connection | None = None


class StatusPayload(typ.TypedDict):
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
                try:
                    await ws.send_media({"type": "random", "payload": str(number)})
                except asyncio.CancelledError:  # noqa: PERF203 - bubble up cancellation
                    raise
                except (ConnectionError, OSError, RuntimeError):
                    # best-effort: drop failed connections silently in this example
                    pass
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

    async def on_disconnect(self, _: WebSocketLike, _close_code: int) -> None:
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


def create_app() -> falcon_asgi.App:
    """Create and configure the Falcon ASGI application."""
    app = LifespanApp()
    install(app)
    conn_mgr = typ.cast(
        "WebSocketConnectionManager",
        getattr(app, "ws_connection_manager"),  # noqa: B009
    )

    controller = WorkerController()

    @app.lifespan
    async def lifespan(app_instance: LifespanApp) -> typ.AsyncIterator[None]:
        global DB
        DB = await _setup_db()
        await controller.start(random_worker, conn_mgr=conn_mgr)
        try:
            yield
        finally:
            await controller.stop()
            with cl.suppress(Exception):
                conns = list(conn_mgr.connections.values())
                if conns:
                    await asyncio.gather(
                        *(ws.close() for ws in conns), return_exceptions=True
                    )
            if DB is not None:
                await DB.close()
                DB = None

    app.add_websocket_route("/ws", lambda: StatusResource(conn_mgr))  # type: ignore[attr-defined]
    app.add_route("/status", StatusEndpoint())
    return app


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(create_app(), host="127.0.0.1", port=8000)
