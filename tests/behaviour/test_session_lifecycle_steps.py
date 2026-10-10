"""Live Falcon ASGI scenarios for the production WebSocket lifecycle."""

from __future__ import annotations

import asyncio
import dataclasses as dc
import typing as typ
from contextlib import AsyncExitStack

import pytest
from pytest_bdd import given, scenario, then, when
from websockets.exceptions import ConnectionClosed, InvalidStatusCode

from falcon_pachinko.testing import WebSocketTestClient
from tests.behaviour._live_server import DisconnectRecorder, LiveServer

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from falcon_pachinko.testing.client import WebSocketSession


_ASSERTION_FAILURE = "lifecycle expectation failed"


@dc.dataclass(slots=True)
class LiveScenario:
    """Hold a real server and client connection for one BDD scenario."""

    event_loop: asyncio.AbstractEventLoop
    server: LiveServer
    client: WebSocketTestClient
    recorder: DisconnectRecorder
    client_contexts: AsyncExitStack | None = None
    session: WebSocketSession | None = None
    observed_close_code: int | None = None
    rejected_handshake: Exception | None = None


@pytest.fixture
def event_loop(
    event_loop_policy: asyncio.AbstractEventLoopPolicy,
) -> cabc.Iterator[asyncio.AbstractEventLoop]:
    """Give Uvicorn and the test client one event loop for their whole session."""
    loop = event_loop_policy.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        yield loop
    finally:
        asyncio.set_event_loop(None)
        loop.close()


@scenario(
    "session_lifecycle.feature",
    "A normal client close runs disconnect once with its close code",
)
def test_client_close() -> None:  # pragma: no cover - scenario registration
    """Register the client-close acceptance scenario."""


@scenario(
    "session_lifecycle.feature",
    "A server close runs disconnect once with its close code",
)
def test_server_close() -> None:  # pragma: no cover - scenario registration
    """Register the server-close acceptance scenario."""


@scenario(
    "session_lifecycle.feature",
    "A handler close runs disconnect once with its close code",
)
def test_handler_close() -> None:  # pragma: no cover - scenario registration
    """Register the resource-close acceptance scenario."""


@scenario(
    "session_lifecycle.feature",
    "Messages are handled before a client close removes membership",
)
def test_client_messages_then_close() -> None:  # pragma: no cover
    """Register the connection-manager membership scenario."""


@scenario(
    "session_lifecycle.feature",
    "A handler failure still runs disconnect cleanup",
)
def test_handler_failure() -> None:  # pragma: no cover - scenario registration
    """Register the handler-failure acceptance scenario."""


@scenario(
    "session_lifecycle.feature",
    "Stopping the server closes an idle accepted session",
)
def test_server_stop_idle() -> None:  # pragma: no cover - scenario registration
    """Register the idle-session server-stop scenario."""


@scenario(
    "session_lifecycle.feature",
    "Stopping the server cancels a blocked handler safely",
)
def test_server_stop_blocked() -> None:  # pragma: no cover - scenario registration
    """Register the blocked-handler cancellation scenario."""


@scenario(
    "session_lifecycle.feature",
    "A rejected handshake does not run disconnect cleanup",
)
def test_rejected_handshake() -> None:  # pragma: no cover - scenario registration
    """Register the rejected-handshake acceptance scenario."""


async def _connect(context: LiveScenario) -> None:
    """Open the production router's mounted session through the test client."""
    if context.client_contexts is None:
        context.client_contexts = AsyncExitStack()
    manager = context.client.connect("/ws/session")
    context.session = await context.client_contexts.enter_async_context(manager)


async def _close_client_context(context: LiveScenario) -> None:
    """Release the client context after server-side lifecycle work has ended."""
    stack = context.client_contexts
    if stack is not None:
        await stack.aclose()
        context.client_contexts = None
        context.session = None


async def _stop_server(context: LiveScenario) -> None:
    """Stop Uvicorn before exact-once assertions observe final state."""
    await context.server.stop()
    await _close_client_context(context)


def _close_code_from_error(error: ConnectionClosed) -> int:
    """Extract the peer's close code from a websockets client error."""
    received = error.rcvd
    if received is not None:
        return received.code
    return error.code


@given("a live Falcon ASGI WebSocket server", target_fixture="context")
def given_live_server(
    event_loop: asyncio.AbstractEventLoop,
    request: pytest.FixtureRequest,
) -> LiveScenario:
    """Start Falcon through Uvicorn and prepare the client and recorder."""
    server = event_loop.run_until_complete(LiveServer.start(event_loop))
    client = WebSocketTestClient(server.base_url, allow_insecure=True)
    context = LiveScenario(
        event_loop=event_loop,
        server=server,
        client=client,
        recorder=server.application.recorder,
    )

    def cleanup() -> None:
        async def finish() -> None:
            if not context.server._stopped:
                await context.server.stop()
            await _close_client_context(context)

        event_loop.run_until_complete(finish())

    request.addfinalizer(cleanup)
    return context


@when("the client closes the session with code 4000", target_fixture="context")
def when_client_closes(context: LiveScenario) -> LiveScenario:
    """Close an active accepted client connection with an application code."""

    async def exercise() -> None:
        await _connect(context)
        assert context.session is not None, _ASSERTION_FAILURE
        await context.session.close(4000)
        conn_id = context.recorder.connection_ids[-1]
        await context.recorder.wait_for_disconnect(conn_id)

    context.event_loop.run_until_complete(exercise())
    return context


@then("disconnect cleanup is recorded once with code 4000")
def then_client_close_recorded(context: LiveScenario) -> None:
    """Stop the server and verify the peer's code reached cleanup once."""
    context.event_loop.run_until_complete(_stop_server(context))
    records = context.recorder.disconnect_records
    assert len(records) == 1, "one accepted client must disconnect exactly once"
    assert records[0][1] == 4000, "cleanup must receive the client's close code"


@when(
    "the connection manager closes the session with code 4002",
    target_fixture="context",
)
def when_manager_closes(context: LiveScenario) -> LiveScenario:
    """Close the live Falcon socket through its registered manager entry."""

    async def exercise() -> None:
        await _connect(context)
        conn_id = context.recorder.connection_ids[-1]
        websocket = context.server.application.manager.websockets[conn_id]
        await websocket.close(4002)
        assert context.session is not None, _ASSERTION_FAILURE
        try:
            await context.session.receive()
        except ConnectionClosed as exc:
            context.observed_close_code = _close_code_from_error(exc)
        await context.recorder.wait_for_disconnect(conn_id)

    context.event_loop.run_until_complete(exercise())
    return context


@then("the client observes close code 4002")
def then_manager_close_seen(context: LiveScenario) -> None:
    """Verify the client received the manager's close code."""
    assert context.observed_close_code == 4002, "client must observe server close"


@then("disconnect cleanup is recorded once")
def then_manager_close_recorded(context: LiveScenario) -> None:
    """Stop Uvicorn and verify the manager close ran disconnect cleanup once."""
    context.event_loop.run_until_complete(_stop_server(context))
    records = context.recorder.disconnect_records
    assert len(records) == 1, "one server close must trigger one cleanup"
    assert isinstance(records[0][1], int), "cleanup must receive a close code"


@when("the handler closes the session", target_fixture="context")
def when_handler_closes(context: LiveScenario) -> LiveScenario:
    """Ask the resource handler to initiate its close frame."""

    async def exercise() -> None:
        await _connect(context)
        assert context.session is not None, _ASSERTION_FAILURE
        await context.session.send_json({"type": "close-me"})
        try:
            await context.session.receive()
        except ConnectionClosed as exc:
            context.observed_close_code = _close_code_from_error(exc)
        conn_id = context.recorder.connection_ids[-1]
        await context.recorder.wait_for_disconnect(conn_id)

    context.event_loop.run_until_complete(exercise())
    return context


@then("the client observes close code 4001")
def then_handler_close_seen(context: LiveScenario) -> None:
    """Verify the handler's close code reached the client."""
    assert context.observed_close_code == 4001, "client must observe handler close"


@then("disconnect cleanup is recorded once with code 4001")
def then_handler_close_recorded(context: LiveScenario) -> None:
    """Stop Uvicorn and verify handler-close cleanup ran once."""
    context.event_loop.run_until_complete(_stop_server(context))
    records = context.recorder.disconnect_records
    assert len(records) == 1, "one handler close must trigger one cleanup"
    assert records[0][1] == 4001, "cleanup must receive the handler close code"


@when(
    "the client exchanges messages and closes the session",
    target_fixture="context",
)
def when_client_exchanges_and_closes(context: LiveScenario) -> LiveScenario:
    """Send multiple frames through the live router before client close."""

    async def exercise() -> None:
        await _connect(context)
        assert context.session is not None, _ASSERTION_FAILURE
        for index in range(3):
            await context.session.send_json({"type": "echo", "index": index})
            reply = await context.session.receive_json()
            assert reply == {"type": "echo", "index": index}, _ASSERTION_FAILURE
        await context.session.close(4000)
        conn_id = context.recorder.connection_ids[-1]
        await context.recorder.wait_for_disconnect(conn_id)

    context.event_loop.run_until_complete(exercise())
    return context


@then("all replies arrive and the manager has no connection")
def then_membership_removed(context: LiveScenario) -> None:
    """Stop the server and verify completed cleanup removed registration."""
    context.event_loop.run_until_complete(_stop_server(context))
    assert context.server.application.manager.websockets == {}, (
        "disconnect cleanup must remove the manager registration"
    )


@when("a handler raises an unexpected error", target_fixture="context")
def when_handler_raises(context: LiveScenario) -> LiveScenario:
    """Trigger a real handler exception on an already accepted WebSocket."""

    async def exercise() -> None:
        await _connect(context)
        assert context.session is not None, _ASSERTION_FAILURE
        await context.session.send_json({"type": "boom"})
        try:
            await context.session.receive()
        except ConnectionClosed as exc:
            context.observed_close_code = _close_code_from_error(exc)
        conn_id = context.recorder.connection_ids[-1]
        await context.recorder.wait_for_disconnect(conn_id)

    context.event_loop.run_until_complete(exercise())
    return context


@then("the client observes close code 1011")
def then_handler_error_close(context: LiveScenario) -> None:
    """Stop the server and check the failure close code sent to the client."""
    context.event_loop.run_until_complete(_stop_server(context))
    assert context.observed_close_code == 1011, "handler failure must close with 1011"


@then("disconnect cleanup is recorded once with code 1011")
def then_handler_error_recorded(context: LiveScenario) -> None:
    """Verify the accepted handler failure still ran one disconnect hook."""
    records = context.recorder.disconnect_records
    assert len(records) == 1, "handler failure must run cleanup exactly once"
    assert records[0][1] == 1011, "cleanup must receive the failure close code"


@then("the original handler error is captured and membership is removed")
def then_original_handler_error(context: LiveScenario) -> None:
    """Check the resource retained its original server-side exception."""
    errors = context.recorder.handler_errors
    assert len(errors) == 1, "the resource must capture one handler exception"
    assert str(errors[0]) == "live WebSocket handler failed", (
        "cleanup must not replace the original handler exception"
    )
    assert context.server.application.manager.websockets == {}, (
        "handler failure cleanup must remove the socket"
    )


@when("the server stops with an idle accepted session", target_fixture="context")
def when_server_stops_idle(context: LiveScenario) -> LiveScenario:
    """Stop Uvicorn while a client remains accepted and idle."""

    async def exercise() -> None:
        await _connect(context)
        conn_id = context.recorder.connection_ids[-1]
        await context.server.stop()
        await context.recorder.wait_for_disconnect(conn_id)
        assert context.session is not None, _ASSERTION_FAILURE
        try:
            await context.session.receive()
        except ConnectionClosed as exc:
            context.observed_close_code = _close_code_from_error(exc)

    context.event_loop.run_until_complete(exercise())
    return context


@then("disconnect cleanup records the close code sent by Uvicorn")
def then_idle_shutdown_code(context: LiveScenario) -> None:
    """Compare the client-observed server close with the cleanup record."""
    context.event_loop.run_until_complete(_close_client_context(context))
    records = context.recorder.disconnect_records
    assert len(records) == 1, "server shutdown must run cleanup once"
    assert records[0][1] == context.observed_close_code, (
        "cleanup must receive Uvicorn's effective close code"
    )


@when("the server stops while a handler is blocked", target_fixture="context")
def when_server_stops_blocked(context: LiveScenario) -> LiveScenario:
    """Cancel a live session task while its resource handler is blocked."""

    async def exercise() -> None:
        await _connect(context)
        assert context.session is not None, _ASSERTION_FAILURE
        await context.session.send_json({"type": "block"})
        await asyncio.wait_for(context.recorder.block_started.wait(), timeout=5)
        conn_id = context.recorder.connection_ids[-1]
        await context.server.stop()
        await context.recorder.wait_for_disconnect(conn_id)

    context.event_loop.run_until_complete(exercise())
    return context


@then("disconnect cleanup is recorded once with code 1001")
def then_cancellation_cleanup(context: LiveScenario) -> None:
    """Verify task cancellation is reported as going away exactly once."""
    context.event_loop.run_until_complete(_close_client_context(context))
    records = context.recorder.disconnect_records
    assert len(records) == 1, "cancelled accepted sessions must clean up once"
    assert records[0][1] == 1001, "cancellation cleanup must use code 1001"


@then("the manager has no connection")
def then_cancelled_membership_removed(context: LiveScenario) -> None:
    """Verify cancellation cannot strand accepted manager membership."""
    assert context.server.application.manager.websockets == {}, (
        "server stop must remove accepted membership"
    )


@when("the client attempts a rejected handshake", target_fixture="context")
def when_rejected_handshake(context: LiveScenario) -> LiveScenario:
    """Attempt a resource handshake that returns false before acceptance."""

    async def exercise() -> None:
        manager = context.client.connect("/ws/reject")
        try:
            async with manager:
                pytest.fail("the rejecting resource must fail the handshake")
        except InvalidStatusCode as exc:
            context.rejected_handshake = exc

    context.event_loop.run_until_complete(exercise())
    return context


@then("the handshake is rejected without disconnect cleanup")
def then_rejection_skips_cleanup(context: LiveScenario) -> None:
    """Stop Uvicorn and verify pre-accept rejection has no disconnect hook."""
    context.event_loop.run_until_complete(_stop_server(context))
    assert context.rejected_handshake is not None, "the handshake must be rejected"
    assert context.recorder.disconnect_records == [], (
        "rejected connections must not run accepted-session cleanup"
    )
    assert context.recorder.rejected_disconnects == 0, (
        "on_disconnect must not run for a rejected handshake"
    )
