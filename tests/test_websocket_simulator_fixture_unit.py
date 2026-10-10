"""Unit tests for the simulator-backed router pytest fixture."""

from __future__ import annotations

import asyncio
import typing as typ

import msgspec.json as msjson
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
        self.received_event = asyncio.Event()
        EchoResource.instances.append(self)

    async def on_connect(
        self, req: falcon.Request, ws: WebSocketLike, **params: object
    ) -> bool:
        """Accept the simulated connection before the receive loop starts."""
        assert isinstance(ws, WebSocketSimulator), (
            "the harness must inject the simulator instance"
        )
        return True

    async def on_unhandled(self, ws: WebSocketLike, message: str | bytes) -> None:
        """Decode and echo the inbound envelope through normal dispatch."""
        assert isinstance(ws, WebSocketSimulator), (
            "the simulator harness must pass the injected socket to dispatch"
        )
        payload = msjson.decode(message)
        self.received.append(payload)
        self.received_event.set()
        await ws.send_json({"type": "ack", "payload": payload})


class GreeterResource(WebSocketResource):
    """Resource that accepts the connection and sends a welcome message."""

    async def on_connect(
        self, req: falcon.Request, ws: WebSocketLike, **params: object
    ) -> bool:
        """Greet the client and accept the simulated connection."""
        assert isinstance(ws, WebSocketSimulator), (
            "the harness must inject the simulator instance"
        )
        await ws.accept()
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


class ClosingResource(WebSocketResource):
    """Close an accepted session from its message handler."""

    instances: typ.ClassVar[list[ClosingResource]] = []

    def __init__(self) -> None:
        self.closed_event = asyncio.Event()
        ClosingResource.instances.append(self)

    async def on_connect(
        self, req: falcon.Request, ws: WebSocketLike, **params: object
    ) -> bool:
        """Keep the session open for the dispatched close frame."""
        return True

    async def on_unhandled(self, ws: WebSocketLike, message: str | bytes) -> None:
        """Close the socket from the active responder session."""
        await ws.close(code=1000)
        self.closed_event.set()


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
            await asyncio.wait_for(resource.received_event.wait(), timeout=1.0)
            assert resource.received == [{"type": "ping"}], (
                "the resource must have received the frame through dispatch"
            )
            assert connection.closed is False, "the accepted session stays open"
            assert connection.accepted is True, "the router must accept the session"
            assert connection.pop_sent_json() == {
                "type": "ack",
                "payload": {"type": "ping"},
            }, "the connection must expose the decoded ack frame"

        assert connection.closed is True, "the disconnect must close the session"
        assert connection.websocket.closed is True, "the peer close must be mirrored"

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

    async def test_inbound_push_inside_context_reaches_dispatch(
        self,
        websocket_simulator: SimulatorRouterHarness,
    ) -> None:
        """A frame pushed after accept is handled by the responder task."""
        EchoResource.instances.clear()
        websocket_simulator.router.add_route("/echo", EchoResource)

        async with websocket_simulator.connect("/echo") as connection:
            resource = EchoResource.instances[-1]
            await connection.push_json({"type": "ping"})
            await asyncio.wait_for(resource.received_event.wait(), timeout=1.0)
            assert resource.received == [{"type": "ping"}], (
                "frames pushed inside the context must reach resource dispatch"
            )

    async def test_responder_failure_propagates_from_context_exit(
        self,
        websocket_simulator: SimulatorRouterHarness,
    ) -> None:
        """A receive-task failure is raised by harness teardown."""
        failure_seen = asyncio.Event()

        class FailingResource(WebSocketResource):
            async def on_connect(
                self, req: falcon.Request, ws: WebSocketLike, **params: object
            ) -> bool:
                return True

            async def on_unhandled(
                self, ws: WebSocketLike, message: str | bytes
            ) -> None:
                failure_seen.set()
                raise RuntimeError

        websocket_simulator.router.add_route("/failure", FailingResource)

        async def run_failing_connection() -> None:
            async with websocket_simulator.connect("/failure") as connection:
                await connection.push_text("trigger")
                await asyncio.wait_for(failure_seen.wait(), timeout=1.0)

        with pytest.raises(RuntimeError):
            await run_failing_connection()

    async def test_startup_failure_is_not_reported_as_a_second_task_error(
        self,
        websocket_simulator: SimulatorRouterHarness,
    ) -> None:
        """The same startup exception is not described as a second failure."""
        failure = RuntimeError("responder failed before accepting")

        class EarlyFailingResource(WebSocketResource):
            async def on_connect(
                self, req: falcon.Request, ws: WebSocketLike, **params: object
            ) -> bool:
                raise failure

        websocket_simulator.router.add_route("/early-failure", EarlyFailingResource)

        with pytest.raises(RuntimeError, match="before accepting") as raised:
            async with websocket_simulator.connect("/early-failure"):
                pytest.fail("a failing responder must fail before yielding a session")

        assert raised.value is failure, "the original startup exception must propagate"
        notes = getattr(raised.value, "__notes__", ())
        assert not any("responder task also failed" in note for note in notes), (
            "the exception must not report itself as an additional task failure"
        )

    async def test_server_initiated_close_stops_the_receive_loop(
        self,
        websocket_simulator: SimulatorRouterHarness,
    ) -> None:
        """A close from a dispatched handler wakes the blocked receiver."""
        websocket_simulator.router.add_route("/close", ClosingResource)

        async with websocket_simulator.connect("/close") as connection:
            resource = ClosingResource.instances[-1]
            await connection.push_text("close")
            await asyncio.wait_for(resource.closed_event.wait(), timeout=1.0)

        assert connection.close_code == 1000, "the handler's close code must persist"
        assert connection.websocket.closed is True, "the peer must be closed too"
