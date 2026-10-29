"""Live envelope validation over the router receive loop."""

import asyncio

import falcon.asgi
import pytest

from falcon_pachinko import (
    WebSocketLike,
    WebSocketResource,
    WebSocketRouter,
    handles_message,
)
from falcon_pachinko.testing import LiveWebSocketServer


class _Resource(WebSocketResource):
    """Record dispatch outcomes through a real connection."""

    def __init__(self, events: asyncio.Queue[tuple[str, object]]) -> None:
        self.events = events

    async def on_typed(self, ws: WebSocketLike, payload: int) -> None:
        """Record a validated integer.

        Parameters
        ----------
        ws : WebSocketLike
            The active connection.
        payload : int
            The converted integer.
        """
        self.events.put_nowait(("typed", payload))

    @handles_message("optional")
    async def optional(self, ws: WebSocketLike, payload: int | None) -> None:
        """Record an optional payload.

        Parameters
        ----------
        ws : WebSocketLike
            The active connection.
        payload : int | None
            The converted payload.
        """
        self.events.put_nowait(("optional", payload))

    async def on_unhandled(self, ws: WebSocketLike, message: str | bytes) -> None:
        """Record a rejected frame.

        Parameters
        ----------
        ws : WebSocketLike
            The active connection.
        message : str | bytes
            The original frame.
        """
        self.events.put_nowait(("fallback", message))


@pytest.mark.asyncio
async def test_live_missing_and_null_payload_validation() -> None:
    """Invalid frames fall back without closing the accepted connection."""
    app = falcon.asgi.App()
    events: asyncio.Queue[tuple[str, object]] = asyncio.Queue()
    router = WebSocketRouter(receive_loop=True)
    router.add_route("/", _Resource, events)
    router.attach(app, "/ws")
    missing = '{"type":"typed"}'
    null = '{"type":"typed","payload":null}'

    async with (
        LiveWebSocketServer(app) as server,
        server.client().connect("/ws") as session,
    ):
        for frame in (
            missing,
            null,
            '{"type":"optional","payload":null}',
            '{"type":"typed","payload":3}',
        ):
            await session.send_text(frame)
        async with asyncio.timeout(5):
            received = [await events.get() for _ in range(4)]

    expected = [
        ("fallback", missing),
        ("fallback", null),
        ("optional", None),
        ("typed", 3),
    ]
    assert received == expected, (
        "invalid payloads must fall back and the connection must remain usable"
    )
