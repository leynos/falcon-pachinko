"""Behavioural tests for the WebSocket simulator integration."""

from __future__ import annotations

import asyncio
import dataclasses as dc
import typing as typ

import msgspec.json as msjson
import pytest
from pytest_bdd import given, scenario, then, when

from falcon_pachinko import SimulatorConnection, WebSocketResource, WebSocketSimulator
from falcon_pachinko.testing import SimulatorRouterHarness

if typ.TYPE_CHECKING:  # pragma: no cover - typing only
    import collections.abc as cabc


class EchoResource(WebSocketResource):
    """Resource that records the injected simulator instance."""

    instances: typ.ClassVar[list[EchoResource]] = []

    def __init__(self) -> None:
        self.websocket: WebSocketSimulator | None = None
        self.received: list[object] = []
        EchoResource.instances.append(self)

    async def on_connect(self, req: object, ws: object, **_: object) -> bool:
        """Capture the simulator; the router owns all later receives."""
        assert isinstance(ws, WebSocketSimulator), (
            "the router must inject the simulator instance"
        )
        self.websocket = ws
        return True

    async def on_unhandled(self, ws: object, message: str | bytes) -> None:
        """Echo one decoded simulator frame as an acknowledgement."""
        payload = msjson.decode(message)
        self.received.append(payload)
        await typ.cast("WebSocketSimulator", ws).send_media({"type": "ack"})


@dc.dataclass(slots=True)
class SimulatorScenario:
    """Container for scenario state."""

    harness: SimulatorRouterHarness
    payload: dict[str, object] | None = None
    simulator: WebSocketSimulator | None = None
    connection: SimulatorConnection | None = None
    resource: EchoResource | None = None


@pytest.fixture
def event_loop() -> cabc.Iterator[asyncio.AbstractEventLoop]:
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
    """Create a mounted harness that injects a simulator per connection."""
    EchoResource.instances.clear()
    harness = SimulatorRouterHarness()
    harness.router.add_route("/echo", EchoResource)
    return SimulatorScenario(harness=harness)


@given(
    'the simulator has a queued message {"type": "ping"}',
    target_fixture="context",
)
def given_message(
    context: SimulatorScenario,
) -> SimulatorScenario:
    """Record the payload to seed before the router starts receiving."""
    context.payload = {"type": "ping"}
    return context


@when(
    'a websocket connection targets "/echo"',
    target_fixture="context",
)
def when_connection(
    context: SimulatorScenario, event_loop: asyncio.AbstractEventLoop
) -> SimulatorScenario:
    """Run the router in the background and wait for its seeded reply."""
    assert context.payload is not None, "the simulator payload must be prepared"

    async def connect() -> SimulatorConnection:
        async with context.harness.connect(
            "/echo", initial_inbound=[(context.payload, "json")]
        ) as connection:
            await asyncio.wait_for(connection.wait_for_sent_count(1), timeout=5)
            return connection

    context.connection = event_loop.run_until_complete(connect())
    context.simulator = context.connection.simulator
    context.resource = EchoResource.instances[-1]
    return context


@then("the resource receives the simulator instance")
def then_resource(context: SimulatorScenario) -> None:
    """Assert that the resource saw the injected simulator."""
    assert context.resource is not None, "the echo resource must have been constructed"
    assert context.resource.websocket is context.simulator, (
        "the resource must receive the injected simulator"
    )
    assert context.resource.received == [{"type": "ping"}], (
        "the resource must have decoded the queued message"
    )


@then("the simulator records the acknowledged message")
def then_ack(context: SimulatorScenario) -> None:
    """Ensure outbound frames are captured by the simulator."""
    assert context.simulator is not None, "the connection must create a simulator"
    assert context.simulator.sent_messages == [{"type": "ack"}], (
        "the simulator must record the acknowledgement frame"
    )
    assert context.simulator.pop_sent() == {"type": "ack"}, (
        "popping the sent frame must return the acknowledgement"
    )


@then("the simulator closes the connection")
def then_closed(context: SimulatorScenario) -> None:
    """Verify the simulator lifecycle is completed by the router."""
    assert context.connection is not None, "the harness must yield a connection"
    assert context.simulator is not None, "the connection must create a simulator"
    assert context.connection.closed is True, "the harness must close the connection"
    assert context.simulator.closed is True, "the simulator must be closed"
    assert context.simulator.close_code == 1000, "the close code must be the default"
