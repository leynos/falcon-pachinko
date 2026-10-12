"""Unit tests for the simulator-backed router pytest fixture."""

from __future__ import annotations

import asyncio
import typing as typ
from types import SimpleNamespace
from unittest import mock

import msgspec.json as msjson
import pytest

from falcon_pachinko import (
    AcceptOptions,
    SimulatorConnection,
    SimulatorRouterHarness,
    WebSocketResource,
    WebSocketSession,
    WebSocketSimulator,
)
from falcon_pachinko._testing_harness import _HarnessSimulator, _OriginalWebSocket

if typ.TYPE_CHECKING:
    import falcon

    from falcon_pachinko.protocols import WebSocketLike


class EchoResource(WebSocketResource):
    """Resource that echoes inbound JSON payloads over an accepted session."""

    instances: typ.ClassVar[list[EchoResource]] = []

    def __init__(self) -> None:
        self.received: list[object] = []
        EchoResource.instances.append(self)

    async def on_connect(
        self, req: falcon.Request, ws: WebSocketLike, **params: object
    ) -> bool:
        """Approve the connection and retain the guarded socket for inspection."""
        del req, params
        self.websocket = ws
        return True

    async def on_unhandled(self, ws: WebSocketLike, message: str | bytes) -> None:
        """Record and acknowledge the seeded message from the router loop."""
        payload = msjson.decode(message)
        self.received.append(payload)
        await typ.cast("WebSocketSimulator", ws).send_json({
            "type": "ack",
            "payload": payload,
        })


class GreeterResource(WebSocketResource):
    """Resource that sends a welcome and waits for a client acknowledgement."""

    closed: typ.ClassVar[asyncio.Event] = asyncio.Event()

    async def on_connect(
        self, req: falcon.Request, ws: WebSocketLike, **params: object
    ) -> bool:
        """Approve the request before the router accepts it."""
        del req, ws, params
        return True

    async def on_session_start(self, ws: WebSocketLike) -> None:
        """Exchange active-session frames and close with a visible code."""
        await ws.send_text("welcome aboard")
        await ws.receive_text()
        await ws.close(code=4004)
        self.closed.set()


class ChattyResource(WebSocketResource):
    """Resource that negotiates a subprotocol and closes with a custom code."""

    disconnect_codes: typ.ClassVar[list[int]] = []

    async def on_connect(
        self, req: falcon.Request, ws: WebSocketLike, **params: object
    ) -> AcceptOptions:
        """Choose ``chat`` while leaving acceptance to the router."""
        del req, ws, params
        return AcceptOptions(subprotocol="chat")

    async def on_session_start(self, ws: WebSocketLike) -> None:
        """Close the accepted session with code 1001."""
        await ws.close(code=1001)

    async def on_disconnect(self, ws: WebSocketLike, close_code: int) -> None:
        """Remember the code delivered by the completed session lifecycle."""
        del ws
        self.disconnect_codes.append(close_code)


class BlockingSetupResource(WebSocketResource):
    """Hold session setup until the harness cancels a blocked connection."""

    instances: typ.ClassVar[list[BlockingSetupResource]] = []
    disconnect_codes: typ.ClassVar[list[int]] = []

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.instances.append(self)

    async def on_connect(
        self, req: falcon.Request, ws: WebSocketLike, **params: object
    ) -> bool:
        """Allow setup to enter the accepted session phase."""
        del req, ws, params
        return True

    async def on_session_start(self, ws: WebSocketLike) -> None:
        """Block setup until the harness exercises bounded shutdown."""
        del ws
        self.started.set()
        await asyncio.Event().wait()

    async def on_disconnect(self, ws: WebSocketLike, close_code: int) -> None:
        """Record the close code after the session is finalized."""
        del ws
        self.disconnect_codes.append(close_code)


@pytest.mark.asyncio
class TestWebSocketSimulatorFixture:
    """Unit tests covering simulator fixture routing and lifecycle mirroring."""

    async def test_overlapping_routes_reuse_staged_simulator(
        self,
        websocket_simulator: SimulatorRouterHarness,
    ) -> None:
        """A failed prefix candidate must not consume the staged simulator."""
        websocket_simulator.router.add_route("/rooms/{room}", EchoResource)
        websocket_simulator.router.add_route("/rooms/{room}/x", EchoResource)

        async with websocket_simulator.connect(
            "/rooms/1/x",
            initial_inbound=[({"type": "ping"}, "json")],
        ) as connection:
            assert connection.accepted is True, (
                "the selected full route must use the staged simulator"
            )
            await asyncio.wait_for(connection.wait_for_sent_count(1), timeout=5)
            assert connection.pop_sent_json() == {
                "type": "ack",
                "payload": {"type": "ping"},
            }, "the selected route must respond through the staged simulator"

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
            await asyncio.wait_for(connection.wait_for_sent_count(1), timeout=5)
            resource = EchoResource.instances[-1]
            assert connection.session is not None, (
                "the simulator must expose its session"
            )
            assert resource.websocket is connection.session.ws, (
                "the resource must receive the guarded session socket"
            )
            assert resource.received == [{"type": "ping"}], (
                "the resource must have received the seeded payload"
            )
            assert connection.closed is False, "the accepted session must stay open"
            assert connection.accepted is True, "the resource must accept the session"
            assert connection.pop_sent_json() == {
                "type": "ack",
                "payload": {"type": "ping"},
            }, "the connection must expose the decoded ack frame"
        assert connection.closed is True, "the fixture must close the connection"
        assert connection.close_code == 1000, "the harness close code must be reported"
        assert connection.websocket.closed is True, (
            "the underlying websocket must be closed after the context"
        )

    async def test_fixture_closes_accepted_connections(
        self,
        websocket_simulator: SimulatorRouterHarness,
    ) -> None:
        """Post-accept code can exchange frames and close the session."""
        websocket_simulator.router.add_route("/greeter", GreeterResource)
        GreeterResource.closed = asyncio.Event()

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
            await connection.push_text("client ready")
            await asyncio.wait_for(GreeterResource.closed.wait(), timeout=5)
            assert connection.closed is True, "session setup should close the socket"
            assert connection.close_code == 4004, "the session-start close code"

        # The explicit session-start close code must survive context cleanup.
        assert connection.websocket.closed is True, (
            "the lifecycle handle must close the underlying websocket"
        )
        assert connection.websocket.close_code == 4004, (
            "the resource close code must be mirrored"
        )
        assert connection.close_code == 4004, "the close code must be preserved"
        assert connection.subprotocol is None, "no subprotocol was negotiated"

    async def test_simulator_connection_subprotocol_and_close_code(
        self,
        websocket_simulator: SimulatorRouterHarness,
    ) -> None:
        """Ensure options and session-start close mirror to the original stub."""
        websocket_simulator.router.add_route("/chat", ChattyResource)
        ChattyResource.disconnect_codes.clear()

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

        assert ChattyResource.disconnect_codes == [1001], (
            "joined cleanup must receive the resource's close code exactly once"
        )
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

    async def test_fixture_exit_cancels_blocked_session_start(
        self,
        websocket_simulator: SimulatorRouterHarness,
    ) -> None:
        """Context exit must finish when post-accept setup never returns."""
        BlockingSetupResource.instances.clear()
        BlockingSetupResource.disconnect_codes.clear()
        websocket_simulator.router.add_route("/blocked", BlockingSetupResource)

        async def exercise() -> SimulatorConnection:
            async with websocket_simulator.connect("/blocked") as connection:
                await asyncio.wait_for(
                    BlockingSetupResource.instances[-1].started.wait(), timeout=5
                )
            return connection

        connection = await asyncio.wait_for(exercise(), timeout=3)

        assert connection.closed is True, "context exit must close the connection"
        assert BlockingSetupResource.disconnect_codes == [1000], (
            "cancellation during teardown must still finalize once"
        )


@pytest.mark.asyncio
async def test_close_connection_retrieves_completed_task_exception() -> None:
    """Harness teardown retrieves a finished run-task exception explicitly."""
    session_task = mock.Mock()
    session_task.done.return_value = True
    session_task.cancelled.return_value = False
    session_task.exception.return_value = RuntimeError("session setup failed")
    session = typ.cast(
        "WebSocketSession",
        SimpleNamespace(
            ws=SimpleNamespace(close=mock.AsyncMock()), aclose=mock.AsyncMock()
        ),
    )

    await SimulatorRouterHarness._close_connection(
        session,
        _HarnessSimulator(),
        _OriginalWebSocket(),
        typ.cast("asyncio.Task[None]", session_task),
    )

    session_task.exception.assert_called_once_with()
