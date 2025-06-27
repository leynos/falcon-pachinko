"""Example server showcasing the ``falcon_pachinko`` extension.

The application exposes an HTTP endpoint and a WebSocket route that
processes ``status`` messages and periodically broadcasts random
numbers. Run the server with ``uvicorn`` and connect using the
accompanying ``client.py`` script.
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
import secrets
import typing

import aiosqlite
import falcon.asgi

# WebSocket disconnection will be handled by generic exceptions
from falcon_pachinko import WebSocketLike, WebSocketResource, handles_message, install


async def _setup_db() -> aiosqlite.Connection:
    conn = await aiosqlite.connect(":memory:")
    await conn.execute("CREATE TABLE status(value TEXT)")
    await conn.execute("INSERT INTO status(value) VALUES('ready')")
    await conn.commit()
    return conn


DB = asyncio.run(_setup_db())


class StatusPayload(typing.TypedDict):
    """Type definition for status message payload."""

    text: str


async def random_worker(ws: WebSocketLike) -> None:
    """
    Periodically sends random numbers to the client over a WebSocket connection.

    Runs indefinitely, sending a JSON message with a random number every 5 seconds
    until cancelled.
    """
    try:
        while True:
            await asyncio.sleep(5)
            number = secrets.randbelow(65536)
            await ws.send_media({"type": "random", "payload": str(number)})
    except (ConnectionError, OSError):
        # Exit if the connection is lost
        pass
    except asyncio.CancelledError:
        pass


class StatusResource(WebSocketResource):
    """WebSocket resource for handling status updates and random number broadcasting."""

    def __init__(self) -> None:
        """Initialize the StatusResource with no active background task."""
        self._task: asyncio.Task[None] | None = None

    async def on_connect(
        self, req: falcon.Request, ws: WebSocketLike, **_: object
    ) -> bool:
        """Handle a new WebSocket connection by accepting it and starting a background
        task.

        Accepts the connection and starts a background task to send random numbers.

        Returns
        -------
            True to indicate the WebSocket connection has been accepted.
        """
        await ws.accept()
        self._task = asyncio.create_task(random_worker(ws))
        return True

    async def on_disconnect(self, ws: WebSocketLike, close_code: int) -> None:
        """Handle cleanup when a WebSocket connection is closed.

        Cancels the background task if it exists when the connection is closed.
        """
        if self._task:
            self._task.cancel()

    @handles_message("status")
    async def update_status(
        self, ws: WebSocketLike, payload: StatusPayload
    ) -> None:
        """
        Handle incoming WebSocket "status" messages to update the stored status value.

        Updates the status in the database with the provided text and sends an
        acknowledgment message back to the client containing the updated value.
        """
        text = payload["text"]
        await DB.execute("UPDATE status SET value=?", (text,))
        await DB.commit()
        await ws.send_media({"type": "ack", "payload": text})


class StatusEndpoint:
    """HTTP endpoint for retrieving the current status value."""

    async def on_get(self, req: falcon.Request, resp: falcon.Response) -> None:
        """
        Handle HTTP GET requests to retrieve the current status value.

        Responds with a JSON object containing the current status text from the
        database under the "status" key. If no status is set, the value is null.
        """
        async with DB.execute("SELECT value FROM status") as cursor:
            row = await cursor.fetchone()
        resp.media = {"status": row[0] if row else None}


def create_app() -> falcon.asgi.App:
    """
    Create and configure the Falcon ASGI application with WebSocket and HTTP routes.

    Returns
    -------
        The configured Falcon ASGI application instance.
    """
    app = falcon.asgi.App()
    install(app)
    app.add_websocket_route("/ws", StatusResource)  # type: ignore[attr-defined]
    app.add_route("/status", StatusEndpoint())
    return app


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(create_app(), host="127.0.0.1", port=8000)
