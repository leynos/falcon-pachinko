import asyncio
import secrets
import typing

import aiosqlite
import falcon.asgi
from falcon.asgi import WebSocketClosedError

from falcon_pachinko import WebSocketResource, handles_message, install


async def _setup_db() -> aiosqlite.Connection:
    conn = await aiosqlite.connect(":memory:")
    await conn.execute("CREATE TABLE status(value TEXT)")
    await conn.execute("INSERT INTO status(value) VALUES('ready')")
    await conn.commit()
    return conn


DB = asyncio.run(_setup_db())


class StatusPayload(typing.TypedDict):
    text: str


async def random_worker(ws: falcon.asgi.WebSocket) -> None:
    try:
        while True:
            await asyncio.sleep(5)
            number = secrets.randbelow(65536)
            await ws.send_media({"type": "random", "payload": str(number)})
    except WebSocketClosedError:
        # Exit if the connection is lost
        pass
    except asyncio.CancelledError:
        pass


class StatusResource(WebSocketResource):
    def __init__(self) -> None:
        self._task: asyncio.Task[None] | None = None

    async def on_connect(
        self, req: falcon.Request, ws: falcon.asgi.WebSocket, **_: typing.Any
    ) -> bool:
        await ws.accept()
        self._task = asyncio.create_task(random_worker(ws))
        return True

    async def on_disconnect(self, ws: falcon.asgi.WebSocket, close_code: int) -> None:
        if self._task:
            self._task.cancel()

    @handles_message("status")
    async def update_status(
        self, ws: falcon.asgi.WebSocket, payload: StatusPayload
    ) -> None:
        text = payload["text"]
        await DB.execute("UPDATE status SET value=?", (text,))
        await DB.commit()
        await ws.send_media({"type": "ack", "payload": text})


class StatusEndpoint:
    async def on_get(self, req: falcon.Request, resp: falcon.Response) -> None:
        async with DB.execute("SELECT value FROM status") as cursor:
            row = await cursor.fetchone()
        resp.media = {"status": row[0] if row else None}


def create_app() -> falcon.asgi.App:
    app = falcon.asgi.App()
    install(app)
    app.add_websocket_route("/ws", StatusResource)
    app.add_route("/status", StatusEndpoint())
    return app


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(create_app(), host="127.0.0.1", port=8000)
