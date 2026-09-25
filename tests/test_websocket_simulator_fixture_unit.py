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

if typ.TYPE_CHECKING:
    import falcon

    from falcon_pachinko.protocols import WebSocketLike


class EchoResource(WebSocketResource):
    """Resource that echoes inbound JSON payloads and closes the connection."""

    instances: typ.ClassVar[list[EchoResource]] = []

    def __init__(self) -> None:
        self.received: list[object] = []
        EchoResource.instances.append(self)

    async def on_connect(
        self, req: falcon.Request, ws: WebSocketLike, **params: object
    ) -> bool:
        """Handle the simulated connection for the test resource."""
        assert isinstance(ws, WebSocketSimulator), (
            "the harness must inject the simulator instance"
        )
        payload = await ws.receive_json(dict)
        self.received.append(payload)
        await ws.send_json({"type": "ack", "payload": payload})
        return False


class GreeterResource(WebSocketResource):
    """Resource that accepts the connection and sends a welcome message."""

    async def on_connect(
        self, req: falcon.Request, ws: WebSocketLike, **params: object
    ) -> bool:
        """Greet the client and accept the simulated connection."""
        assert isinstance(ws, WebSocketSimulator), (
            "the harness must inject the simulator instance"
        )
        await ws.send_text("welcome aboard")
        return True


class ChattyResource(WebSocketResource):
    """Resource that negotiates a subprotocol and closes with a custom code."""

    async def on_connect(
        self, req: falcon.Request, ws: WebSocketLike, **params: object
    ) -> bool:
        """Accept the connection using ``chat`` and close with code 1001."""
        await ws.accept(subprotocol="chat")
        await ws.close(code=1001)
        return False


@pytest.mark.asyncio
class TestWebSocketSimulatorFixture:
    """Unit tests covering simulator fixture routing and lifecycle mirroring."""

    async def test_fixture_routes_connections(
        self,
        websocket_simulator: SimulatorRouterHarness,
    ) -> None:
        """Ensure the fixture injects the simulator and captures frames."""
        EchoResource.instances.clear()
        websocket_simulator.router.add_route("/echo", EchoResource)
        initial_frames: list[tuple[object, typ.Literal["json"]]] = [
            ({"type": "ping"}, "json"),
        ]

        async with websocket_simulator.connect(
            "/echo",
            initial_inbound=initial_frames,
        ) as connection:
            assert isinstance(connection, SimulatorConnection), (
                "connect() must yield a SimulatorConnection"
            )
            resource = EchoResource.instances[-1]
            assert resource.received == [{"type": "ping"}], (
                "the resource must have received the seeded payload"
            )
            assert connection.closed is True, "the connection must be closed"
            assert connection.accepted is False, "the connection must not be accepted"
            assert connection.pop_sent_json() == {
                "type": "ack",
                "payload": {"type": "ping"},
            }, "the connection must expose the decoded ack frame"
            assert connection.websocket.closed is True, (
                "the underlying websocket must be closed"
            )

    async def test_fixture_closes_accepted_connections(
        self,
        websocket_simulator: SimulatorRouterHarness,
    ) -> None:
        """Accepted connections remain open during the context and are tidied up."""
        websocket_simulator.router.add_route("/greeter", GreeterResource)

        async with websocket_simulator.connect("/greeter") as connection:
            assert isinstance(connection, SimulatorConnection), (
                "connect() must yield a SimulatorConnection"
            )
            assert connection.accepted is True, "an accepting resource must accept"
            assert connection.closed is False, (
                "the connection must stay open in-context"
            )
            assert connection.subprotocol is None, "no subprotocol was negotiated"
            assert connection.close_code is None, "the connection is not yet closed"
            assert connection.pop_sent() == "welcome aboard", (
                "the greeter must have sent its welcome message"
            )

        # After leaving the context the fixture should close the simulator.
        assert connection.closed is True, "the fixture must close the connection"
        assert connection.websocket.closed is True, (
            "the fixture must close the underlying websocket"
        )
        assert connection.websocket.close_code == 1000, (
            "the default close code must be used"
        )
        assert connection.close_code == 1000, "the default close code must be mirrored"
        assert connection.subprotocol is None, "no subprotocol was negotiated"

    async def test_simulator_connection_subprotocol_and_close_code(
        self,
        websocket_simulator: SimulatorRouterHarness,
    ) -> None:
        """Ensure lifecycle metadata mirrors between simulator and original stub."""
        websocket_simulator.router.add_route("/chat", ChattyResource)

        async with websocket_simulator.connect("/chat") as connection:
            assert connection.accepted is True, (
                "the resource must accept the connection"
            )
            assert connection.subprotocol == "chat", "the negotiated subprotocol"
            assert connection.close_code == 1001, "the resource-chosen close code"
            assert connection.websocket.subprotocol == "chat", (
                "the underlying websocket must mirror the subprotocol"
            )
            assert connection.websocket.close_code == 1001, (
                "the underlying websocket must mirror the close code"
            )
            assert connection.websocket.accepted is True, (
                "the underlying websocket must be accepted"
            )
            assert connection.closed is True, "the connection must be closed"

        assert connection.subprotocol == "chat", "the subprotocol must persist"
        assert connection.close_code == 1001, "the close code must persist"
        assert connection.websocket.subprotocol == "chat", (
            "the underlying websocket subprotocol must persist"
        )
        assert connection.websocket.close_code == 1001, (
            "the underlying websocket close code must persist"
        )
        assert connection.websocket.closed is True, (
            "the underlying websocket must remain closed"
        )
        assert connection.websocket.accepted is True, (
            "the underlying websocket must remain accepted"
        )
