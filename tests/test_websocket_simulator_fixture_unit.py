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
from falcon_pachinko._testing_harness import _HarnessSimulator


def _failed_future(error: Exception) -> asyncio.Future[None]:
    """Return an awaitable that raises ``error`` when awaited."""
    future = asyncio.get_running_loop().create_future()
    future.set_exception(error)
    return future


if typ.TYPE_CHECKING:
    import collections.abc as cabc

    import falcon

    from falcon_pachinko.protocols import WebSocketLike


class EchoResource(WebSocketResource):
    """Resource that echoes inbound JSON payloads over an accepted session."""

    instances: typ.ClassVar[list[EchoResource]] = []

    def __init__(self) -> None:
        self.received: list[object] = []
        self.websocket: WebSocketLike | None = None
        EchoResource.instances.append(self)

    async def on_connect(
        self, req: falcon.Request, ws: WebSocketLike, **params: object
    ) -> bool:
        """Approve the connection and leave receive ownership to the router."""
        self.websocket = ws
        del req, params
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
    """Resource that sends a welcome message after router acceptance."""

    async def on_connect(
        self, req: falcon.Request, ws: WebSocketLike, **params: object
    ) -> bool:
        """Approve the connection for router acceptance."""
        del req, ws, params
        return True

    async def on_session_start(self, ws: WebSocketLike) -> None:
        """Greet the accepted client before the receive loop starts."""
        await ws.send_text("welcome aboard")


class ChattyResource(WebSocketResource):
    """Resource that negotiates a subprotocol and closes with a custom code."""

    disconnect_codes: typ.ClassVar[list[int]] = []

    async def on_connect(
        self, req: falcon.Request, ws: WebSocketLike, **params: object
    ) -> bool:
        """Accept the connection using ``chat`` and close with code 1001."""
        await ws.accept(subprotocol="chat")
        await ws.close(code=1001)
        return False

    async def on_disconnect(self, ws: WebSocketLike, close_code: int) -> None:
        """Remember the code delivered by the completed session lifecycle."""
        del ws
        self.disconnect_codes.append(close_code)


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
            await asyncio.wait_for(connection.wait_for_sent_count(1), timeout=5)
            resource = EchoResource.instances[-1]
            assert connection.session is not None, (
                "accepted connections retain a session"
            )
            assert resource.websocket is connection.session.ws, (
                "resources receive the guarded session socket"
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

    @pytest.mark.parametrize("operation_name", ["accept", "close"])
    async def test_readiness_is_signalled_when_operation_fails(
        self, monkeypatch: pytest.MonkeyPatch, operation_name: str
    ) -> None:
        """A failed handshake operation wakes readiness waiters with its error."""
        operation_error = RuntimeError()

        def fail_operation(
            self: WebSocketSimulator, *_: object
        ) -> asyncio.Future[None]:
            del self
            return _failed_future(operation_error)

        monkeypatch.setattr(WebSocketSimulator, operation_name, fail_operation)
        simulator = _HarnessSimulator()

        with pytest.raises(RuntimeError) as caught:
            await getattr(simulator, operation_name)()

        assert caught.value is operation_error, (
            f"the {operation_name} failure must be preserved"
        )
        assert simulator.ready_event.is_set(), (
            f"{operation_name} failures must still wake readiness waiters"
        )

    async def test_readiness_failure_survives_router_cleanup_timeout(
        self,
        websocket_simulator: SimulatorRouterHarness,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Cleanup errors must not replace the original readiness failure."""
        readiness_error = LookupError()
        cleanup_error = TimeoutError()

        def fail_readiness(
            simulator: WebSocketSimulator,
            router_task: asyncio.Task[None],
        ) -> asyncio.Future[None]:
            del simulator, router_task
            return _failed_future(readiness_error)

        def fail_cleanup(router_task: asyncio.Task[None]) -> asyncio.Future[None]:
            future = asyncio.get_running_loop().create_future()

            def finish_cleanup(task: asyncio.Task[None]) -> None:
                if not task.cancelled():
                    task.exception()
                future.set_exception(cleanup_error)

            router_task.add_done_callback(finish_cleanup)
            return future

        monkeypatch.setattr(websocket_simulator, "_wait_until_ready", fail_readiness)
        monkeypatch.setattr(websocket_simulator, "_await_router_task", fail_cleanup)
        websocket_simulator.router.add_route("/ready", ClosingResource)

        with pytest.raises(LookupError) as caught:
            async with websocket_simulator.connect("/ready"):
                pytest.fail("connect must not yield after readiness failed")
        assert caught.value is readiness_error, (
            "cleanup timeout must not replace the readiness failure"
        )

    async def test_router_task_cancel_timeout_uses_session_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A cancellation timeout is reported as the harness shutdown error."""

        async def wait_forever() -> None:
            await asyncio.Event().wait()

        router_task = asyncio.create_task(wait_forever())
        first_timeout = TimeoutError()
        cancellation_timeout = TimeoutError()
        wait_calls = 0

        def fail_wait(
            awaitable: cabc.Awaitable[object], *, timeout: float
        ) -> asyncio.Future[None]:
            del awaitable, timeout
            nonlocal wait_calls
            wait_calls += 1
            if wait_calls == 1:
                return _failed_future(first_timeout)
            router_task.cancel()
            return _failed_future(cancellation_timeout)

        monkeypatch.setattr(
            "falcon_pachinko.testing.harness.asyncio.wait_for", fail_wait
        )

        with pytest.raises(
            TimeoutError, match="router session did not stop after the simulator closed"
        ) as caught:
            await SimulatorRouterHarness._await_router_task(router_task)

        await asyncio.gather(router_task, return_exceptions=True)
        assert caught.value.__cause__ is first_timeout, (
            "the session timeout must retain the first shutdown timeout"
        )

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
        ClosingResource.instances.clear()
        websocket_simulator.router.add_route("/close", ClosingResource)

        async with websocket_simulator.connect("/close") as connection:
            resource = ClosingResource.instances[-1]
            await connection.push_text("close")
            await asyncio.wait_for(resource.closed_event.wait(), timeout=1.0)

        assert connection.close_code == 1000, "the handler's close code must persist"
        assert connection.websocket.closed is True, "the peer must be closed too"
