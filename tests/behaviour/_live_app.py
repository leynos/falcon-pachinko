"""Small real Falcon app used by live WebSocket acceptance scenarios."""

from __future__ import annotations

import asyncio
import dataclasses as dc
import typing as typ

import falcon.asgi

from falcon_pachinko import WebSocketResource, WebSocketRouter

if typ.TYPE_CHECKING:
    from falcon_pachinko.protocols import WebSocketLike


@dc.dataclass(slots=True)
class LiveAppState:
    """Capture application-visible messages and connection teardown."""

    received: list[str] = dc.field(default_factory=list)
    recorded_event: asyncio.Event = dc.field(default_factory=asyncio.Event)
    disconnected: asyncio.Event = dc.field(default_factory=asyncio.Event)
    disconnect_code: int | None = None


class LiveWebSocketResource(WebSocketResource):
    """Exercise connect, frame handlers, and disconnect over a real socket."""

    def __init__(self, shared: LiveAppState) -> None:
        self.shared = shared

    async def on_connect(
        self, req: object, ws: WebSocketLike, **params: object
    ) -> bool:
        """Accept and greet the real client before router session dispatch."""
        del req, params
        await ws.accept()
        await ws.send_media({"type": "welcome", "message": "welcome from Falcon"})
        return True

    async def on_echo(self, ws: WebSocketLike, payload: str) -> None:
        """Reply to an echo frame from the connected client."""
        await ws.send_media({"type": "echo", "payload": payload})

    async def on_record(self, ws: WebSocketLike, payload: str) -> None:
        """Record a client frame in app state and acknowledge it."""
        self.shared.received.append(payload)
        self.shared.recorded_event.set()
        await ws.send_media({"type": "recorded", "payload": payload})

    async def on_explode(self, ws: WebSocketLike, payload: str) -> None:
        """Raise from the real responder so the harness captures the failure."""
        del ws, payload
        msg = "intentional live WebSocket responder failure"
        raise RuntimeError(msg)

    async def on_disconnect(self, ws: WebSocketLike, close_code: int) -> None:
        """Publish the close code and signal that the server session ended."""
        del ws
        self.shared.disconnect_code = close_code
        self.shared.disconnected.set()


def build_live_app(shared: LiveAppState) -> falcon.asgi.App:
    """Build an ASGI app using Falcon-Pachinko's supported attach path."""
    app = falcon.asgi.App()
    router = WebSocketRouter()
    router.add_route("/socket", LiveWebSocketResource, shared=shared)
    router.attach(app, "/ws")
    return app
