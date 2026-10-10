"""Unit tests for the simulator-backed router pytest fixture."""

from __future__ import annotations

import asyncio
import typing as typ

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

    async def test_readiness_is_signalled_when_accept_fails(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A failed accept wakes harness readiness waiters with its error."""
        accept_error = RuntimeError()

        def fail_accept(self: WebSocketSimulator, *_: object) -> asyncio.Future[None]:
            del self
            return _failed_future(accept_error)

        monkeypatch.setattr(WebSocketSimulator, "accept", fail_accept)
        simulator = _HarnessSimulator()

        with pytest.raises(RuntimeError) as caught:
            await simulator.accept()

        assert caught.value is accept_error, "the accept failure must be preserved"
        assert simulator.ready_event.is_set(), (
            "accept failures must still wake readiness waiters"
        )

    async def test_readiness_is_signalled_when_close_fails(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A failed close wakes harness readiness waiters with its error."""
        close_error = RuntimeError()

        def fail_close(self: WebSocketSimulator, *_: object) -> asyncio.Future[None]:
            del self
            return _failed_future(close_error)

        monkeypatch.setattr(WebSocketSimulator, "close", fail_close)
        simulator = _HarnessSimulator()

        with pytest.raises(RuntimeError) as caught:
            await simulator.close()

        assert caught.value is close_error, "the close failure must be preserved"
        assert simulator.ready_event.is_set(), (
            "close failures must still wake readiness waiters"
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

        with pytest.raises(LookupError) as caught:
            async with websocket_simulator.connect("/missing"):
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
