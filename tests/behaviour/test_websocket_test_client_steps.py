"""Behavioural tests for the WebSocketTestClient helper."""

from __future__ import annotations

import asyncio
import dataclasses as dc
import typing as typ

import pytest
import websockets.server as ws_server
from pytest_bdd import given, scenario, then, when

from falcon_pachinko.testing import TraceEvent, WebSocketTestClient

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from websockets.typing import Subprotocol


@dc.dataclass(slots=True)
class EchoRecord:
    """Track handshake data and frames received by the echo server."""

    paths: list[str]
    headers: list[dict[str, str]]
    messages: list[object]
    subprotocols: list[str | None]


@dc.dataclass(slots=True)
class ClientContext:
    """Shared scenario context for exercising the test client."""

    event_loop: asyncio.AbstractEventLoop
    server: typ.Any
    base_url: str
    record: EchoRecord
    client: WebSocketTestClient
    response: object | None = None
    trace: list[TraceEvent] | None = None


def _bound_base_url(server: ws_server.WebSocketServer) -> str:
    """Return the ``ws://`` base URL for the first socket bound by *server*."""
    sockets = tuple(server.sockets)
    if not sockets:
        msg = "echo server did not bind any sockets"
        raise RuntimeError(msg)
    host, port, *_ = sockets[0].getsockname()
    return f"ws://{host}:{port}"


@pytest.fixture
def event_loop(
    event_loop_policy: asyncio.AbstractEventLoopPolicy,
) -> cabc.Iterator[asyncio.AbstractEventLoop]:
    """Provide a dedicated event loop per scenario."""
    loop = event_loop_policy.new_event_loop()
    try:
        asyncio.set_event_loop(loop)
        yield loop
    finally:
        asyncio.set_event_loop(None)
        loop.close()


@pytest.fixture
def echo_service(event_loop: asyncio.AbstractEventLoop) -> cabc.Iterator[ClientContext]:
    """Run an echo server for the duration of a scenario."""
    record = EchoRecord(paths=[], headers=[], messages=[], subprotocols=[])
    protocols: list[Subprotocol] = [typ.cast("Subprotocol", "json")]

    async def handler(websocket: ws_server.WebSocketServerProtocol, path: str) -> None:
        record.paths.append(path)
        record.headers.append(dict(websocket.request_headers))
        record.subprotocols.append(websocket.subprotocol)
        async for message in websocket:
            record.messages.append(message)
            await websocket.send(message)

    server = event_loop.run_until_complete(
        ws_server.serve(handler, "127.0.0.1", 0, subprotocols=protocols)
    )
    base_url = _bound_base_url(server)
    client = WebSocketTestClient(
        base_url,
        default_headers={"X-Test": "bdd"},
        subprotocols=protocols,
        capture_trace=True,
        allow_insecure=True,
    )

    context = ClientContext(
        event_loop=event_loop,
        server=server,
        base_url=base_url,
        record=record,
        client=client,
    )

    try:
        yield context
    finally:
        server.close()
        event_loop.run_until_complete(server.wait_closed())


@scenario(
    "websocket_test_client.feature",
    "round-trip JSON payload with trace logging",
)
def test_websocket_test_client() -> None:  # pragma: no cover - bdd registration
    """Scenario registration for the websocket test client feature."""


@given("a running websocket echo service", target_fixture="context")
def given_echo_service(echo_service: ClientContext) -> ClientContext:
    """Return the prepared echo service context."""
    return echo_service


@when(
    'the test client sends a JSON payload to "/echo"',
    target_fixture="context",
)
def when_send_json(context: ClientContext) -> ClientContext:
    """Send a JSON payload using the test client and capture the response."""

    async def exercise() -> None:
        async with context.client.connect("/echo") as session:
            await session.send_json({"type": "ping"})
            context.response = await session.receive_json()
            context.trace = session.trace

    context.event_loop.run_until_complete(exercise())
    return context


@then("the server records the handshake metadata")
def then_server_metadata(context: ClientContext) -> None:
    """Assert the server observed the negotiated headers and subprotocol."""
    assert context.record.paths == ["/echo"], "the server must record the request path"
    headers = {key.lower(): value for key, value in context.record.headers[0].items()}
    assert headers["x-test"] == "bdd", "the custom header must reach the server"
    assert context.record.subprotocols == ["json"], (
        "the negotiated subprotocol must be recorded"
    )


@then("the client observes the echoed payload")
def then_client_observes(context: ClientContext) -> None:
    """Assert the client received the echoed JSON payload."""
    assert context.response == {"type": "ping"}, (
        "the client must observe the echoed payload"
    )


@then("the session trace records the frames")
def then_trace(context: ClientContext) -> None:
    """Verify that the trace contains both the sent and received frames."""
    assert context.trace is not None, "the client must capture a trace"
    assert [event.index for event in context.trace] == [0, 1, 2], (
        "trace events must be indexed in order"
    )
    assert [event.direction for event in context.trace] == [
        "send",
        "receive",
        "close",
    ], "trace events must record send, receive, then close"
    assert [event.kind for event in context.trace] == ["json", "json", "close"], (
        "trace events must record their frame kind"
    )
    assert [event.payload for event in context.trace[:2]] == [
        {"type": "ping"},
        {"type": "ping"},
    ], "the send and receive frames must carry the ping payload"
    assert context.trace[-1].payload == {"code": 1000, "reason": ""}, (
        "the close frame must carry the default close payload"
    )
