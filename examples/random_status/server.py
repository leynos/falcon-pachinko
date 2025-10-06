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
import inspect
import secrets
import typing as typ

import aiosqlite
import falcon.asgi as falcon_asgi

from falcon_pachinko import (
    WebSocketConnectionManager,
    WebSocketLike,
    WebSocketResource,
    WebSocketRouter,
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


class StatusPayload(typ.TypedDict):
    """Type definition for status message payload."""

    text: str


class ServiceContainer:
    """Minimal container used to demonstrate router-level DI wiring."""

    def __init__(self) -> None:
        self._services: dict[str, object] = {}

    def register(self, name: str, value: object) -> None:
        """Expose ``value`` for resources requesting ``name``."""
        self._services[name] = value

    def resolve(self, name: str) -> object:
        """Return the registered dependency named ``name``."""
        try:
            return self._services[name]
        except KeyError as exc:  # pragma: no cover - used interactively
            msg = f"service {name!r} is not registered"
            raise RuntimeError(msg) from exc

    def create_resource(
        self, route_factory: typ.Callable[..., WebSocketResource]
    ) -> WebSocketResource:
        """Instantiate ``route_factory`` injecting registered dependencies."""
        target = getattr(route_factory, "func", route_factory)
        args = getattr(route_factory, "args", ())
        kwargs = dict(getattr(route_factory, "keywords", {}) or {})
        signature = inspect.signature(target)

        for parameter in signature.parameters.values():
            if parameter.name in {"self"}:
                continue
            if parameter.kind in (
                inspect.Parameter.VAR_POSITIONAL,
                inspect.Parameter.VAR_KEYWORD,
            ):
                continue
            if parameter.name in kwargs:
                continue
            if parameter.name in self._services:
                kwargs[parameter.name] = self._services[parameter.name]

        return target(*args, **kwargs)


@worker
async def random_worker(*, conn_mgr: WebSocketConnectionManager) -> None:
    """Periodically broadcast random numbers to all connections."""
    try:
        while True:
            await asyncio.sleep(5)
            number = secrets.randbelow(65536)
            async for ws in conn_mgr.connections():
                # best-effort: drop failed connections silently in this example
                with cl.suppress(ConnectionError, OSError, RuntimeError):
                    await ws.send_media({"type": "random", "payload": str(number)})
    except asyncio.CancelledError:
        # Graceful shutdown: exit on cancellation
        return


class StatusResource(WebSocketResource):
    """WebSocket resource for handling status updates."""

    def __init__(
        self,
        *,
        conn_mgr: WebSocketConnectionManager,
        db: aiosqlite.Connection,
    ) -> None:
        self._conn_mgr = conn_mgr
        self._db = db
        self._conn_id: str | None = None

    async def on_connect(
        self, req: falcon.Request, ws: WebSocketLike, **_: object
    ) -> bool:
        """Accept the connection and register it with the manager."""
        await ws.accept()
        conn_id = secrets.token_hex(16)
        await self._conn_mgr.add_connection(conn_id, ws)
        self._conn_id = conn_id
        return True

    async def on_disconnect(self, _: WebSocketLike, _close_code: int) -> None:
        """Unregister the connection on disconnect."""
        if self._conn_id:
            await self._conn_mgr.remove_connection(self._conn_id)

    @handles_message("status")
    async def update_status(self, ws: WebSocketLike, payload: StatusPayload) -> None:
        """Update the stored status value and acknowledge."""
        text = payload["text"]
        await self._db.execute("UPDATE status SET value=?", (text,))
        await self._db.commit()
        await ws.send_media({"type": "ack", "payload": text})


class StatusEndpoint:
    """HTTP endpoint for retrieving the current status value."""

    def __init__(self, container: ServiceContainer) -> None:
        self._container = container

    async def on_get(self, req: falcon.Request, resp: falcon.Response) -> None:
        """Return the current status value as JSON."""
        conn = typ.cast("aiosqlite.Connection", self._container.resolve("db"))
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
    container = ServiceContainer()
    container.register("conn_mgr", conn_mgr)

    controller = WorkerController()

    @app.lifespan
    async def lifespan(app_instance: LifespanApp) -> typ.AsyncIterator[None]:
        db = await _setup_db()
        container.register("db", db)
        await controller.start(random_worker, conn_mgr=conn_mgr)
        try:
            yield
        finally:
            await controller.stop()
            conns = [ws async for ws in conn_mgr.connections()]
            if conns:
                await asyncio.gather(
                    *(ws.close() for ws in conns), return_exceptions=True
                )
            await db.close()

    router = WebSocketRouter(resource_factory=container.create_resource)
    router.add_route("/", StatusResource)
    router.mount("/ws")
    app.add_websocket_route("/ws", router)
    app.add_route("/status", StatusEndpoint(container))
    return app


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(create_app(), host="127.0.0.1", port=8000)
