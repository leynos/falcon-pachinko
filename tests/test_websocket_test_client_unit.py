"""Unit tests for the WebSocketTestClient helper."""

from __future__ import annotations

import dataclasses as dc
import typing as typ
from contextlib import asynccontextmanager

import pytest
import pytest_asyncio
import websockets.server as ws_server

from falcon_pachinko.testing import TraceEvent, WebSocketTestClient


@dc.dataclass
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
) -> typ.AsyncIterator[tuple[str, EchoState]]:
    """Start an echo server and yield its base URL and captured state."""
    state = EchoState(paths=[], headers=[], messages=[], subprotocols=[])

    async def handler(websocket: ws_server.WebSocketServerProtocol, path: str) -> None:
        await _echo_handler(websocket, path, state)

    server = await ws_server.serve(handler, "127.0.0.1", 0, subprotocols=subprotocols)
    sock = next(iter(server.sockets))
    host, port, *_ = sock.getsockname()
    base_url = f"ws://{host}:{port}"

    try:
        yield base_url, state
    finally:
        server.close()
        await server.wait_closed()


@pytest_asyncio.fixture
async def echo_server() -> typ.AsyncIterator[tuple[str, EchoState]]:
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

    assert reply == {"hello": "world"}
    assert state.messages == ['{"hello":"world"}']
    assert state.paths == ["/chat"]


@pytest.mark.asyncio
async def test_send_and_receive_binary(echo_server: tuple[str, EchoState]) -> None:
    """Exchange binary frames using the helper."""
    base_url, state = echo_server
    client = WebSocketTestClient(base_url, allow_insecure=True)

    payload = b"\x00\x01binary"

    async with client.connect("/binary") as session:
        await session.send_bytes(payload)
        reply = await session.receive_bytes()

    assert reply == payload
    assert state.messages == [payload]
    assert state.paths == ["/binary"]


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
    assert headers["x-app"] == "test"
    assert headers["x-trace"] == "1"


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

    assert reply == "ping"
    assert state.subprotocols == ["trace"]


@pytest.mark.asyncio
async def test_trace_records_send_and_receive(
    echo_server: tuple[str, EchoState],
) -> None:
    """Trace logs capture frame ordering and payloads."""
    base_url, state = echo_server
    client = WebSocketTestClient(base_url, capture_trace=True, allow_insecure=True)

    async with client.connect("/trace") as session:
        await session.send_text("hi")
        await session.receive_text()
        trace = session.trace or []

    assert [event.kind for event in trace] == ["text", "text", "close"]
    assert [event.direction for event in trace] == ["send", "receive", "close"]
    assert [event.payload for event in trace[:2]] == ["hi", "hi"]
    assert trace[-1].payload == {"code": 1000, "reason": ""}
    assert all(isinstance(event, TraceEvent) for event in trace)


@pytest.mark.asyncio
async def test_receive_json_with_custom_type(
    echo_server: tuple[str, EchoState],
) -> None:
    """Structured JSON decoding uses msgspec's typed decoding."""
    base_url, _ = echo_server
    client = WebSocketTestClient(base_url, allow_insecure=True)

    @dc.dataclass
    class Payload:
        message: str

    async with client.connect("/typed") as session:
        await session.send_json({"message": "hello"})
        reply = await session.receive_json(Payload)

    assert isinstance(reply, Payload)
    assert reply.message == "hello"


def test_insecure_base_url_requires_opt_in() -> None:
    """Disallow insecure websocket URLs without explicit opt-in."""
    with pytest.raises(
        ValueError, match="Insecure websocket URLs require allow_insecure=True"
    ):
        WebSocketTestClient("ws://localhost:8765")
