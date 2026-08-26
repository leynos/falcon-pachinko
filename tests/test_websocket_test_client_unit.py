"""Unit tests for the WebSocketTestClient helper."""

from __future__ import annotations

import dataclasses as dc
import typing as typ
from contextlib import asynccontextmanager

import pytest
import pytest_asyncio
import websockets.server as ws_server
from websockets.typing import Subprotocol

from falcon_pachinko.testing import TraceEvent, WebSocketTestClient

if typ.TYPE_CHECKING:
    import collections.abc as cabc


@dc.dataclass(slots=True)
class EchoState:
    """Track events observed by the echo server."""

    paths: list[str]
    headers: list[dict[str, str]]
    messages: list[object]
    subprotocols: list[str | None]


async def _echo_handler(
    websocket: ws_server.WebSocketServerProtocol,
    path: str,
    state: EchoState,
) -> None:
    """Echo incoming frames and capture handshake metadata."""
    state.paths.append(path)
    state.headers.append(dict(websocket.request_headers))
    state.subprotocols.append(websocket.subprotocol)

    async for message in websocket:
        state.messages.append(message)
        await websocket.send(message)


@asynccontextmanager
async def start_echo_server(
    *,
    subprotocols: tuple[str, ...] = (),
) -> cabc.AsyncIterator[tuple[str, EchoState]]:
    """Start an echo server and yield its base URL and captured state."""
    state = EchoState(paths=[], headers=[], messages=[], subprotocols=[])

    async def handler(websocket: ws_server.WebSocketServerProtocol, path: str) -> None:
        await _echo_handler(websocket, path, state)

    protocols = [Subprotocol(proto) for proto in subprotocols]
    server = await ws_server.serve(handler, "127.0.0.1", 0, subprotocols=protocols)
    sockets = tuple(server.sockets)
    if not sockets:
        msg = "echo server did not bind any sockets"
        raise RuntimeError(msg)
    host, port, *_ = sockets[0].getsockname()
    base_url = f"ws://{host}:{port}"

    try:
        yield base_url, state
    finally:
        server.close()
        await server.wait_closed()


@pytest_asyncio.fixture
async def echo_server() -> cabc.AsyncIterator[tuple[str, EchoState]]:
    """Yield a running websocket echo server."""
    async with start_echo_server() as context:
        yield context


@pytest.mark.asyncio
async def test_send_and_receive_json(echo_server: tuple[str, EchoState]) -> None:
    """Send JSON payloads and receive decoded responses."""
    base_url, state = echo_server
    client = WebSocketTestClient(base_url, allow_insecure=True)

    async with client.connect("/chat") as session:
        await session.send_json({"hello": "world"})
        reply = await session.receive_json()

    assert reply == {"hello": "world"}, "the JSON reply must round-trip unchanged"
    assert state.messages == ['{"hello":"world"}'], (
        "the server must have received the encoded JSON payload"
    )
    assert state.paths == ["/chat"], "the server must record the request path"


@pytest.mark.asyncio
async def test_send_and_receive_binary(echo_server: tuple[str, EchoState]) -> None:
    """Exchange binary frames using the helper."""
    base_url, state = echo_server
    client = WebSocketTestClient(base_url, allow_insecure=True)

    payload = b"\x00\x01binary"

    async with client.connect("/binary") as session:
        await session.send_bytes(payload)
        reply = await session.receive_bytes()

    assert reply == payload, "the binary reply must round-trip unchanged"
    assert state.messages == [payload], (
        "the server must have received the binary payload"
    )
    assert state.paths == ["/binary"], "the server must record the request path"


@pytest.mark.asyncio
async def test_header_merging(echo_server: tuple[str, EchoState]) -> None:
    """Default headers merge with per-connection overrides."""
    base_url, state = echo_server
    client = WebSocketTestClient(
        base_url,
        default_headers={"X-App": "test"},
        allow_insecure=True,
    )

    async with client.connect("/headers", headers={"X-Trace": "1"}):
        pass

    headers = {key.lower(): value for key, value in state.headers[0].items()}
    assert headers["x-app"] == "test", "default headers must reach the server"
    assert headers["x-trace"] == "1", "per-connection headers must reach the server"


@pytest.mark.asyncio
async def test_subprotocol_negotiation() -> None:
    """Subprotocol preferences propagate to the server."""
    async with start_echo_server(subprotocols=("trace", "chat")) as (base_url, state):
        client = WebSocketTestClient(
            base_url,
            subprotocols=("trace", "chat"),
            allow_insecure=True,
        )

        async with client.connect("/rooms") as session:
            await session.send_text("ping")
            reply = await session.receive_text()

    assert reply == "ping", "the text reply must round-trip unchanged"
    assert state.subprotocols == ["trace"], (
        "the higher-priority subprotocol must be negotiated"
    )


@pytest.mark.asyncio
async def test_trace_records_send_and_receive(
    echo_server: tuple[str, EchoState],
) -> None:
    """Trace logs capture frame ordering and payloads."""
    base_url, _ = echo_server
    client = WebSocketTestClient(base_url, capture_trace=True, allow_insecure=True)

    async with client.connect("/trace") as session:
        await session.send_text("hi")
        await session.receive_text()
        trace = session.trace or []

    assert [event.index for event in trace] == [0, 1, 2], (
        "trace events must be indexed in order"
    )
    assert [event.kind for event in trace] == ["text", "text", "close"], (
        "trace events must record their frame kind"
    )
    assert [event.direction for event in trace] == ["send", "receive", "close"], (
        "trace events must record send, receive, then close"
    )
    assert [event.payload for event in trace[:2]] == ["hi", "hi"], (
        "the send and receive frames must carry the text payload"
    )
    assert trace[-1].payload == {"code": 1000, "reason": ""}, (
        "the close frame must carry the default close payload"
    )
    assert all(isinstance(event, TraceEvent) for event in trace), (
        "every trace entry must be a TraceEvent"
    )


@pytest.mark.asyncio
async def test_receive_json_with_custom_type(
    echo_server: tuple[str, EchoState],
) -> None:
    """Structured JSON decoding uses msgspec's typed decoding."""
    base_url, _ = echo_server
    client = WebSocketTestClient(base_url, allow_insecure=True)

    @dc.dataclass(slots=True)
    class Payload:
        message: str

    async with client.connect("/typed") as session:
        await session.send_json({"message": "hello"})
        reply = await session.receive_json(Payload)

    assert isinstance(reply, Payload), "the reply must decode into the given type"
    assert reply.message == "hello", "the decoded payload must preserve the message"


def test_insecure_base_url_requires_opt_in() -> None:
    """Disallow insecure websocket URLs without explicit opt-in."""
    with pytest.raises(
        ValueError, match="Insecure websocket URLs require allow_insecure=True"
    ):
        WebSocketTestClient("ws://localhost:8765")
