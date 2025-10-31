"""Behavioural tests for the simulator-backed router pytest fixture."""

from __future__ import annotations

import dataclasses as dc
import typing as typ

import pytest
from pytest_bdd import given, scenario, then, when

from falcon_pachinko import SimulatorConnection, WebSocketResource, WebSocketSimulator

if typ.TYPE_CHECKING:  # pragma: no cover - typing only
    import asyncio

    from falcon_pachinko.testing import SimulatorRouterHarness


@scenario("simulator_fixture.feature", "Simulated harness handles lifecycle")
def test_simulator_fixture() -> None:  # pragma: no cover - scenario registration
    """Scenario registration for pytest-bdd."""


@pytest.fixture
def event_loop(
    event_loop_policy: asyncio.AbstractEventLoopPolicy,
) -> typ.Iterator[asyncio.AbstractEventLoop]:
    """Provide an isolated event loop for behaviour scenarios."""
    loop = event_loop_policy.new_event_loop()
    try:
        yield loop
    finally:
        loop.close()


class EchoResource(WebSocketResource):
    """Resource used to exercise simulator interactions."""

    instances: typ.ClassVar[list[EchoResource]] = []

    def __init__(self) -> None:
        self.received: list[object] = []
        EchoResource.instances.append(self)

    async def on_connect(
        self, req: object, ws: WebSocketSimulator, **params: object
    ) -> bool:
        """Handle the echo interaction for the simulator scenario."""
        payload = await ws.receive_json(dict)
        self.received.append(payload)
        await ws.send_json({"type": "ack", "payload": payload})
        return False


@dc.dataclass
class FixtureContext:
    """Store shared state for simulator fixture scenarios."""

    harness: SimulatorRouterHarness
    payload: dict[str, object] | None = None
    connection: SimulatorConnection | None = None
    resource: EchoResource | None = None


@given("a websocket simulator harness", target_fixture="context")
def given_harness(
    websocket_simulator: SimulatorRouterHarness,
) -> FixtureContext:
    """Prepare the harness and register the echo route."""
    EchoResource.instances.clear()
    websocket_simulator.router.add_route("/echo", EchoResource)
    return FixtureContext(harness=websocket_simulator)


@given('the next connection is seeded with {"type": "ping"}', target_fixture="context")
def given_seed(context: FixtureContext) -> FixtureContext:
    """Record the payload used to seed the simulator."""
    context.payload = {"type": "ping"}
    return context


@when('we connect to "/echo"', target_fixture="context")
def when_connect(
    context: FixtureContext, event_loop: asyncio.AbstractEventLoop
) -> FixtureContext:
    """Establish a connection using the harness fixture."""
    assert context.payload is not None

    async def _open() -> SimulatorConnection:
        async with context.harness.connect(
            "/echo",
            initial_inbound=[(context.payload, "json")],
        ) as connection:
            return connection

    connection = event_loop.run_until_complete(_open())
    context.connection = connection
    context.resource = EchoResource.instances[-1]
    return context


@then("the resource should receive the queued payload")
def then_resource_received(context: FixtureContext) -> None:
    """Assert that the echo resource consumed the seeded payload."""
    assert context.resource is not None
    assert context.resource.received == [{"type": "ping"}]


@then("the simulator helper exposes the ack frame")
def then_simulator_records_ack(context: FixtureContext) -> None:
    """Ensure the helper decodes outbound JSON frames."""
    assert context.connection is not None
    assert context.connection.pop_sent_json() == {
        "type": "ack",
        "payload": {"type": "ping"},
    }


@then("the connection is closed by the fixture")
def then_fixture_closes(context: FixtureContext) -> None:
    """Verify the fixture performs connection teardown automatically."""
    assert context.connection is not None
    assert context.connection.closed is True
    assert context.connection.websocket.closed is True
