"""Focused live session lifecycle support."""

from __future__ import annotations

import asyncio

import pytest
from pytest_bdd import scenario, then, when
from websockets.exceptions import ConnectionClosed, InvalidStatusCode

from tests.behaviour._session_context import (
    LiveScenario,
    close_code_from_error,
    connect,
    stop_server,
)

_ASSERTION_FAILURE = "lifecycle expectation failed"


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
    "A send after handler closure preserves the disconnect code",
)
def test_handler_send_after_close() -> None:
    """Register Falcon's send-side disconnect as a normal transport ending."""


@scenario(
    "session_lifecycle.feature",
    "Messages are handled before a client close removes membership",
)
def test_client_messages_then_close() -> None:  # pragma: no cover
    """Register the connection-manager membership scenario."""


@scenario(
    "session_lifecycle.feature",
    "A rejected handshake does not run disconnect cleanup",
)
def test_rejected_handshake() -> None:  # pragma: no cover - scenario registration
    """Register the rejected-handshake acceptance scenario."""


@scenario(
    "session_lifecycle.feature",
    "Rejecting by close does not run disconnect cleanup",
)
def test_rejected_handshake_closed_by_resource() -> None:
    """Register explicit pre-accept closure as a rejection."""


@scenario(
    "session_lifecycle.feature",
    "Acceptance followed immediately by close still runs cleanup",
)
def test_resource_accepts_then_closes_on_connect() -> None:
    """Register resource-owned acceptance followed by immediate closure."""


@when("the client closes the session with code 4000", target_fixture="context")
def when_client_closes(context: LiveScenario) -> LiveScenario:
    """Close an active accepted client connection with an application code."""

    async def exercise() -> None:
        await connect(context)
        assert context.session is not None, _ASSERTION_FAILURE
        await asyncio.wait_for(context.session.close(4000), timeout=5)
        conn_id = context.recorder.connection_ids[-1]
        await context.recorder.wait_for_disconnect(conn_id)

    context.event_loop.run_until_complete(exercise())
    return context


@when(
    "the connection manager closes the session with code 4002",
    target_fixture="context",
)
def when_manager_closes(context: LiveScenario) -> LiveScenario:
    """Close the live Falcon socket through its registered manager entry."""

    async def exercise() -> None:
        await connect(context)
        conn_id = context.recorder.connection_ids[-1]
        websocket = context.server.application.manager.websockets[conn_id]
        await asyncio.wait_for(websocket.close(4002), timeout=5)
        assert context.session is not None, _ASSERTION_FAILURE
        try:
            await asyncio.wait_for(context.session.receive(), timeout=5)
        except ConnectionClosed as exc:
            context.observed_close_code = close_code_from_error(exc)
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
    context.event_loop.run_until_complete(stop_server(context))
    records = context.recorder.disconnect_records
    assert len(records) == 1, "one server close must trigger one cleanup"
    assert context.recorder.disconnect_invocations == 1, (
        "on_disconnect must be invoked exactly once"
    )
    assert records[0][1] == 4002, (
        "cleanup must receive the effective server-initiated close code"
    )


@when("the handler closes the session", target_fixture="context")
def when_handler_closes(
    context: LiveScenario, message_type: str = "close-me"
) -> LiveScenario:
    """Ask the resource handler to initiate its close frame."""

    async def exercise() -> None:
        await connect(context)
        assert context.session is not None, _ASSERTION_FAILURE
        await asyncio.wait_for(
            context.session.send_json({"type": message_type}), timeout=5
        )
        try:
            await asyncio.wait_for(context.session.receive(), timeout=5)
        except ConnectionClosed as exc:
            context.observed_close_code = close_code_from_error(exc)
        conn_id = context.recorder.connection_ids[-1]
        await context.recorder.wait_for_disconnect(conn_id)

    context.event_loop.run_until_complete(exercise())
    return context


@when("the handler sends after closing the session", target_fixture="context")
def when_handler_sends_after_closing(context: LiveScenario) -> LiveScenario:
    """Exercise the native Falcon exception raised by sending after a close."""
    return when_handler_closes(context, "close-and-send")


@then("the client observes close code 4001")
def then_handler_close_seen(context: LiveScenario) -> None:
    """Verify the handler's close code reached the client."""
    assert context.observed_close_code == 4001, "client must observe handler close"


@then("disconnect cleanup is recorded once with code 4001")
def then_handler_close_recorded(context: LiveScenario) -> None:
    """Stop Uvicorn and verify handler-close cleanup ran once."""
    context.event_loop.run_until_complete(stop_server(context))
    records = context.recorder.disconnect_records
    assert len(records) == 1, "one handler close must trigger one cleanup"
    assert context.recorder.disconnect_invocations == 1, (
        "on_disconnect must be invoked exactly once"
    )
    assert records[0][1] == 4001, "cleanup must receive the handler close code"


@when(
    "the client exchanges messages and closes the session",
    target_fixture="context",
)
def when_client_exchanges_and_closes(context: LiveScenario) -> LiveScenario:
    """Send multiple frames through the live router before client close."""

    async def exercise() -> None:
        await connect(context)
        assert context.session is not None, _ASSERTION_FAILURE
        for index in range(3):
            await asyncio.wait_for(
                context.session.send_json({"type": "echo", "index": index}), timeout=5
            )
            reply = await asyncio.wait_for(context.session.receive_json(), timeout=5)
            assert reply == {"type": "echo", "index": index}, _ASSERTION_FAILURE
        await asyncio.wait_for(context.session.close(4000), timeout=5)
        conn_id = context.recorder.connection_ids[-1]
        await context.recorder.wait_for_disconnect(conn_id)

    context.event_loop.run_until_complete(exercise())
    return context


@then("all replies arrive and the manager has no connection")
def then_membership_removed(context: LiveScenario) -> None:
    """Stop the server and verify completed cleanup removed registration."""
    context.event_loop.run_until_complete(stop_server(context))
    assert context.server.application.manager.websockets == {}, (
        "disconnect cleanup must remove the manager registration"
    )
    assert context.recorder.disconnect_invocations == 1, (
        "message exchange must still invoke on_disconnect exactly once"
    )


@when("the client attempts a rejected handshake", target_fixture="context")
def when_rejected_handshake(
    context: LiveScenario, path: str = "/ws/reject"
) -> LiveScenario:
    """Attempt a resource handshake that returns false before acceptance."""

    async def exercise() -> None:
        try:
            await connect(context, path)
            pytest.fail("the rejecting resource must fail the handshake")
        except InvalidStatusCode as exc:
            context.rejected_handshake = exc

    context.event_loop.run_until_complete(exercise())
    return context


@when("the resource closes the handshake without accepting", target_fixture="context")
def when_resource_rejects_by_close(context: LiveScenario) -> LiveScenario:
    """Use native Falcon closure to reject before acceptance."""
    return when_rejected_handshake(context, "/ws/reject-close")


@when(
    "the resource accepts and closes during connection setup", target_fixture="context"
)
def when_resource_accepts_then_closes(context: LiveScenario) -> LiveScenario:
    """Observe an accepted handshake and close from inside on_connect."""

    async def exercise() -> None:
        await connect(context, "/ws/accept-close")
        assert context.session is not None, _ASSERTION_FAILURE
        try:
            await asyncio.wait_for(context.session.receive_json(), timeout=5)
        except ConnectionClosed as exc:
            context.observed_close_code = close_code_from_error(exc)
        conn_id = context.recorder.connection_ids[-1]
        await context.recorder.wait_for_disconnect(conn_id)

    context.event_loop.run_until_complete(exercise())
    return context


@then("the client observes close code 4003")
def then_setup_close_seen(context: LiveScenario) -> None:
    """Verify the resource's setup close code reached the client."""
    assert context.observed_close_code == 4003, "client must observe setup closure"


@then("disconnect cleanup is recorded once with code 4003")
def then_setup_close_cleanup(context: LiveScenario) -> None:
    """Join the session before asserting resource-owned closure cleanup."""
    context.event_loop.run_until_complete(stop_server(context))
    assert [code for _, code in context.recorder.disconnect_records] == [4003], (
        "acceptance followed by closure must clean up exactly once"
    )
    assert context.recorder.disconnect_invocations == 1, (
        "accepted setup closure must invoke on_disconnect exactly once"
    )


@then("the handshake is rejected without disconnect cleanup")
def then_rejection_skips_cleanup(context: LiveScenario) -> None:
    """Stop Uvicorn and verify pre-accept rejection has no disconnect hook."""
    context.event_loop.run_until_complete(stop_server(context))
    assert context.rejected_handshake is not None, "the handshake must be rejected"
    assert context.recorder.disconnect_records == [], (
        "rejected connections must not run accepted-session cleanup"
    )
    assert context.recorder.disconnect_invocations == 0, (
        "a rejected handshake must never invoke on_disconnect"
    )
    assert context.recorder.rejected_disconnects == 0, (
        "on_disconnect must not run for a rejected handshake"
    )
