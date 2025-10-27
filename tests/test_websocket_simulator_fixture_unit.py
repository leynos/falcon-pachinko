"""Unit tests for the simulator-backed router pytest fixture."""

from __future__ import annotations

import typing as typ

import pytest

from falcon_pachinko import (
    SimulatorConnection,
    SimulatorRouterHarness,
    WebSocketResource,
    WebSocketSimulator,
)


class EchoResource(WebSocketResource):
    """Resource that echoes inbound JSON payloads and closes the connection."""

    instances: typ.ClassVar[list[EchoResource]] = []

    def __init__(self) -> None:
        self.received: list[object] = []
        EchoResource.instances.append(self)

    async def on_connect(
        self, req: object, ws: WebSocketSimulator, **params: object
    ) -> bool:
        """Handle the simulated connection for the test resource."""
        payload = await ws.receive_json(dict)
        self.received.append(payload)
        await ws.send_json({"type": "ack", "payload": payload})
        return False


class GreeterResource(WebSocketResource):
    """Resource that accepts the connection and sends a welcome message."""

    async def on_connect(
        self, req: object, ws: WebSocketSimulator, **params: object
    ) -> bool:
        """Greet the client and accept the simulated connection."""
        await ws.send_text("welcome aboard")
        return True


@pytest.mark.asyncio
async def test_fixture_routes_connections(
    websocket_simulator: SimulatorRouterHarness,
) -> None:
    """Ensure the fixture injects the simulator and captures frames."""
    EchoResource.instances.clear()
    websocket_simulator.router.add_route("/echo", EchoResource)

    async with websocket_simulator.connect(
        "/echo",
        initial_inbound=[({"type": "ping"}, "json")],
    ) as connection:
        assert isinstance(connection, SimulatorConnection)
        resource = EchoResource.instances[-1]
        assert resource.received == [{"type": "ping"}]
        assert connection.closed is True
        assert connection.accepted is False
        assert connection.pop_sent_json() == {
            "type": "ack",
            "payload": {"type": "ping"},
        }
        assert connection.websocket.closed is True


@pytest.mark.asyncio
async def test_fixture_closes_accepted_connections(
    websocket_simulator: SimulatorRouterHarness,
) -> None:
    """Accepted connections remain open during the context and are tidied up."""
    websocket_simulator.router.add_route("/greeter", GreeterResource)

    async with websocket_simulator.connect("/greeter") as connection:
        assert isinstance(connection, SimulatorConnection)
        assert connection.accepted is True
        assert connection.closed is False
        assert connection.pop_sent() == "welcome aboard"

    # After leaving the context the fixture should close the simulator.
    assert connection.closed is True
    assert connection.websocket.closed is True
    assert connection.websocket.close_code == 1000
