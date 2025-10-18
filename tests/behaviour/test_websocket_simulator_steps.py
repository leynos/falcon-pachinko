"""Behavioural tests for the WebSocket simulator integration."""

from __future__ import annotations

import asyncio
import dataclasses as dc
import typing as typ

import msgspec.json as msjson
import pytest
from pytest_bdd import given, scenario, then, when

from falcon_pachinko import WebSocketResource, WebSocketRouter, WebSocketSimulator


class OriginalWebSocket:
    """Minimal stub representing the ASGI-provided websocket."""

    def __init__(self) -> None:
        self.accepted = False
        self.closed = False
        self.close_code: int | None = None
        self.sent: list[object] = []

    async def accept(
        self, subprotocol: str | None = None
    ) -> None:  # pragma: no cover - unused
        """Record handshake acceptance."""
        self.accepted = True

    async def close(self, code: int = 1000) -> None:
        """Record closure metadata."""
        self.closed = True
        self.close_code = code

    async def send_media(self, data: object) -> None:  # pragma: no cover - unused
        """Record sent data."""
        self.sent.append(data)

    async def receive_media(self) -> object:  # pragma: no cover - unused
        """Return a placeholder payload."""
        return None


class EchoResource(WebSocketResource):
    """Resource that records the injected simulator instance."""

    instances: typ.ClassVar[list[EchoResource]] = []

    def __init__(self) -> None:
        self.websocket: WebSocketSimulator | None = None
        self.received: list[object] = []
        EchoResource.instances.append(self)

    async def on_connect(self, req: object, ws: object, **_: object) -> bool:
        """Capture the simulator and record the first inbound message."""
        simulator = typ.cast("WebSocketSimulator", ws)
        self.websocket = simulator
        raw = await simulator.receive_media()
        if isinstance(raw, bytes | bytearray | memoryview):
            buffer = bytes(raw)
        else:
            buffer = typ.cast("str", raw).encode("utf-8")
        payload = msjson.decode(buffer)
        self.received.append(payload)
        await simulator.send_media({"type": "ack"})
        return False


@dc.dataclass
class SimulatorScenario:
    """Container for scenario state."""

    router: WebSocketRouter
    simulator: WebSocketSimulator
    resource: EchoResource | None = None
    original: OriginalWebSocket | None = None


@pytest.fixture
def event_loop() -> typ.Iterator[asyncio.AbstractEventLoop]:
    """Provide an event loop isolated from pytest-asyncio's global loop."""
    loop = asyncio.new_event_loop()
    try:
        yield loop
    finally:
        loop.close()


@scenario("websocket_simulator.feature", "router injects simulator connections")
def test_websocket_simulator() -> None:  # pragma: no cover - scenario registration
    """Scenario registration for simulator injection."""


@given(
    "a router configured with a simulator factory",
    target_fixture="context",
)
def given_router() -> SimulatorScenario:
    """Create a router that always injects the same simulator instance."""
    EchoResource.instances.clear()
    simulator = WebSocketSimulator()
    router = WebSocketRouter(simulator_factory=lambda *_: simulator)
    router.add_route("/echo", EchoResource)
    router.mount("/")
    return SimulatorScenario(router=router, simulator=simulator)


@given(
    'the simulator has a queued message {"type": "ping"}',
    target_fixture="context",
)
def given_message(
    context: SimulatorScenario, event_loop: asyncio.AbstractEventLoop
) -> SimulatorScenario:
    """Queue a payload that the resource will consume during connect."""
    event_loop.run_until_complete(context.simulator.push_json({"type": "ping"}))
    return context


@when(
    'a websocket connection targets "/echo"',
    target_fixture="context",
)
def when_connection(
    context: SimulatorScenario, event_loop: asyncio.AbstractEventLoop
) -> SimulatorScenario:
    """Dispatch a connection through the router."""
    req = type("Req", (), {"path": "/echo", "path_template": ""})()
    original = OriginalWebSocket()
    event_loop.run_until_complete(context.router.on_websocket(req, original))
    context.original = original
    context.resource = EchoResource.instances[-1]
    return context


@then("the resource receives the simulator instance")
def then_resource(context: SimulatorScenario) -> None:
    """Assert that the resource saw the injected simulator."""
    assert context.resource is not None
    assert context.resource.websocket is context.simulator
    assert context.resource.received == [{"type": "ping"}]


@then("the simulator records the acknowledged message")
def then_ack(context: SimulatorScenario) -> None:
    """Ensure outbound frames are captured by the simulator."""
    assert context.simulator.sent_messages == [{"type": "ack"}]
    assert context.simulator.pop_sent() == {"type": "ack"}


@then("the simulator closes the connection")
def then_closed(context: SimulatorScenario) -> None:
    """Verify the simulator lifecycle is completed by the router."""
    assert context.simulator.closed is True
    assert context.simulator.close_code == 1000
