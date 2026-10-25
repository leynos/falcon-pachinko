"""Real Falcon sockets preserve raw input and custom outgoing serialization."""

from __future__ import annotations

import asyncio
import dataclasses as dc
import typing as typ

import falcon
import falcon.asgi
import falcon.media
import pytest

from falcon_pachinko import (
    WebSocketResource,
    WebSocketRouter,
    configure_raw_frame_media,
)
from falcon_pachinko.testing import LiveWebSocketServer

if typ.TYPE_CHECKING:
    from falcon_pachinko import WebSocketLike


@dc.dataclass(slots=True)
class _ReceivedFrames:
    """Keep separate observations at dispatch and fallback boundaries."""

    dispatched: asyncio.Queue[str | bytes] = dc.field(default_factory=asyncio.Queue)
    unhandled: asyncio.Queue[str | bytes] = dc.field(default_factory=asyncio.Queue)


class _RecordingResource(WebSocketResource):
    """Exercise ordinary dispatch before observing its raw fallback payload."""

    def __init__(self, received: _ReceivedFrames) -> None:
        """Bind this connection's observations to the test-owned queues."""
        self.received = received

    @typ.override
    async def dispatch(self, ws: WebSocketLike, raw: str | bytes) -> None:
        """Record exact frame type and value before ordinary schema dispatch."""
        self.received.dispatched.put_nowait(raw)
        await super().dispatch(ws, raw)

    @typ.override
    async def on_unhandled(self, ws: WebSocketLike, message: str | bytes) -> None:
        """Record the unmodified frame that cannot match an envelope handler."""
        self.received.unhandled.put_nowait(message)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "frame",
    ["plain text", '"quoted JSON string"', b"\xff\x00not-json"],
    ids=["plain-text", "quoted-json-string", "non-json-binary"],
)
async def test_live_router_dispatch_preserves_exact_raw_frames(
    frame: str | bytes,
) -> None:
    """Identity media handlers preserve real TEXT and BINARY frame contents."""
    app = falcon.asgi.App()
    received = _ReceivedFrames()
    router = WebSocketRouter()
    router.add_route("/", _RecordingResource, received)
    router.attach(app, "/ws")

    async with LiveWebSocketServer(app) as server:
        client = server.client()
        async with client.connect("/ws") as session:
            await session.send(frame)
            async with asyncio.timeout(5):
                dispatched = await received.dispatched.get()
                unhandled = await received.unhandled.get()

    assert type(dispatched) is type(frame), (
        "dispatch must receive the original frame type"
    )
    assert dispatched == frame, "dispatch must receive the exact frame payload"
    assert type(unhandled) is type(frame), (
        "fallback must retain the original frame type"
    )
    assert unhandled == frame, "fallback handling must retain quotes and non-JSON bytes"


class _CustomTextHandler(falcon.media.TextBaseHandlerWS):
    """Make the original application's outgoing TEXT format observable."""

    @typ.override
    def serialize(self, media: object) -> str:
        """Use a recognisable application-specific TEXT format."""
        return f"custom-text:{media}"


class _CustomBinaryHandler(falcon.media.BinaryBaseHandlerWS):
    """Make the original application's outgoing BINARY format observable."""

    @typ.override
    def serialize(self, media: object) -> bytes:
        """Use a recognisable application-specific BINARY format."""
        return f"custom-binary:{media}".encode()


@dc.dataclass(slots=True)
class _OrdinaryRoute:
    """Observe app-wide media input on a route outside Pachinko's router."""

    received: asyncio.Queue[object]

    async def on_websocket(
        self, req: falcon.asgi.Request, ws: falcon.asgi.WebSocket
    ) -> None:
        """Receive through configured media and send with both original encoders."""
        await ws.accept()
        async with asyncio.timeout(5):
            self.received.put_nowait(await ws.receive_media())
        await ws.send_media("reply", payload_type=falcon.WebSocketPayloadType.TEXT)
        await ws.send_media("reply", payload_type=falcon.WebSocketPayloadType.BINARY)
        await ws.close()


@pytest.mark.asyncio
async def test_live_ordinary_route_retains_both_custom_outgoing_serializers() -> None:
    """Opt-in applies app-wide while preserving original send_media encoders."""
    app = falcon.asgi.App()
    app.ws_options.media_handlers = {
        falcon.WebSocketPayloadType.TEXT: _CustomTextHandler(),
        falcon.WebSocketPayloadType.BINARY: _CustomBinaryHandler(),
    }
    configure_raw_frame_media(app)
    received: asyncio.Queue[object] = asyncio.Queue()
    app.add_route("/ordinary", _OrdinaryRoute(received))

    async with LiveWebSocketServer(app) as server:
        client = server.client()
        async with client.connect("/ordinary") as session:
            await session.send_text('"app-wide raw"')
            async with asyncio.timeout(5):
                raw = await received.get()
                text = await session.receive_text()
                binary = await session.receive_bytes()

    assert raw == '"app-wide raw"', "ordinary routes must also receive raw media"
    assert text == "custom-text:reply", "the original TEXT format must be preserved"
    assert binary == b"custom-binary:reply", (
        "the original BINARY format must be preserved"
    )


@dc.dataclass(slots=True)
class _DecodedRoute:
    """Capture Falcon's default decoded media without raw configuration."""

    received: asyncio.Queue[object]

    async def on_websocket(
        self, req: falcon.asgi.Request, ws: falcon.asgi.WebSocket
    ) -> None:
        """Expose the decoded value received from the real socket."""
        await ws.accept()
        async with asyncio.timeout(5):
            self.received.put_nowait(await ws.receive_media())
        await ws.close()


@pytest.mark.asyncio
async def test_live_unconfigured_app_retains_default_decoded_media() -> None:
    """Raw preservation requires explicit application setup."""
    app = falcon.asgi.App()
    received: asyncio.Queue[object] = asyncio.Queue()
    app.add_route("/decoded", _DecodedRoute(received))

    async with LiveWebSocketServer(app) as server:
        client = server.client()
        async with client.connect("/decoded") as session:
            await session.send_text('{"value":1}')
            async with asyncio.timeout(5):
                decoded = await received.get()

    assert decoded == {"value": 1}, "unconfigured receive_media must still decode JSON"
