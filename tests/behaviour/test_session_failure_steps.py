"""Exercise handler failure and server shutdown through real Falcon sessions.

The feature bindings assert cleanup with 1011 for unexpected handler failures,
the Uvicorn-reported code for idle shutdown, and 1001 for cancellation during
blocked handlers. Each scenario verifies cleanup through recorded effects.
"""

from __future__ import annotations

import asyncio
import typing as typ

from pytest_bdd import scenario, then, when
from websockets.exceptions import ConnectionClosed

from tests.behaviour._session_context import (
    LiveScenario,
    close_client_context,
    close_code_from_error,
    connect,
    stop_server,
)

if typ.TYPE_CHECKING:
    import pytest

_ASSERTION_FAILURE = "lifecycle expectation failed"


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


@when("a handler raises an unexpected error", target_fixture="context")
def when_handler_raises(context: LiveScenario) -> LiveScenario:
    """Trigger a real handler exception on an already accepted WebSocket."""

    async def exercise() -> None:
        await connect(context)
        assert context.session is not None, _ASSERTION_FAILURE
        await asyncio.wait_for(context.session.send_json({"type": "boom"}), timeout=5)
        try:
            await asyncio.wait_for(context.session.receive(), timeout=5)
        except ConnectionClosed as exc:
            context.observed_close_code = close_code_from_error(exc)
        conn_id = context.recorder.connection_ids[-1]
        await context.recorder.wait_for_disconnect(conn_id)

    context.event_loop.run_until_complete(exercise())
    return context


@then("the client observes close code 1011")
def then_handler_error_close(context: LiveScenario) -> None:
    """Stop the server and check the failure close code sent to the client."""
    context.event_loop.run_until_complete(stop_server(context))
    assert context.observed_close_code == 1011, "handler failure must close with 1011"


@then("disconnect cleanup is recorded once with code 1011")
def then_handler_error_recorded(context: LiveScenario) -> None:
    """Verify the accepted handler failure still ran one disconnect hook."""
    context.event_loop.run_until_complete(stop_server(context))
    records = context.recorder.disconnect_records
    assert len(records) == 1, "handler failure must run cleanup exactly once"
    assert context.recorder.disconnect_invocations == 1, (
        "handler failure must invoke on_disconnect exactly once"
    )
    assert records[0][1] == 1011, "cleanup must receive the failure close code"


@then("the original handler error is captured and membership is removed")
def then_original_handler_error(
    context: LiveScenario, caplog: pytest.LogCaptureFixture
) -> None:
    """Check Falcon reported the original server-side exception after cleanup."""
    errors = context.recorder.handler_errors
    assert len(errors) == 1, "the resource must capture one handler exception"
    assert str(errors[0]) == "live WebSocket handler failed", (
        "cleanup must not replace the original handler exception"
    )
    assert any(
        record.exc_info is not None and record.exc_info[1] is errors[0]
        for record in caplog.records
    ), "Falcon must report the original handler exception"
    assert context.server.application.manager.websockets == {}, (
        "handler failure cleanup must remove the socket"
    )


@when("the server stops with an idle accepted session", target_fixture="context")
def when_server_stops_idle(context: LiveScenario) -> LiveScenario:
    """Stop Uvicorn while a client remains accepted and idle."""

    async def exercise() -> None:
        await connect(context)
        conn_id = context.recorder.connection_ids[-1]
        await context.server.stop()
        await context.recorder.wait_for_disconnect(conn_id)
        assert context.session is not None, _ASSERTION_FAILURE
        try:
            await asyncio.wait_for(context.session.receive(), timeout=5)
        except ConnectionClosed as exc:
            context.observed_close_code = close_code_from_error(exc)

    context.event_loop.run_until_complete(exercise())
    return context


@then("disconnect cleanup records the close code sent by Uvicorn")
def then_idle_shutdown_code(context: LiveScenario) -> None:
    """Compare the client-observed server close with the cleanup record."""
    context.event_loop.run_until_complete(stop_server(context))
    context.event_loop.run_until_complete(close_client_context(context))
    records = context.recorder.disconnect_records
    assert len(records) == 1, "server shutdown must run cleanup once"
    assert context.recorder.disconnect_invocations == 1, (
        "idle shutdown must invoke on_disconnect exactly once"
    )
    assert records[0][1] == context.observed_close_code, (
        "cleanup must receive Uvicorn's effective close code"
    )


@when("the server stops while a handler is blocked", target_fixture="context")
def when_server_stops_blocked(context: LiveScenario) -> LiveScenario:
    """Cancel a live session task while its resource handler is blocked."""

    async def exercise() -> None:
        await connect(context)
        assert context.session is not None, _ASSERTION_FAILURE
        await asyncio.wait_for(context.session.send_json({"type": "block"}), timeout=5)
        await asyncio.wait_for(context.recorder.block_started.wait(), timeout=5)
        conn_id = context.recorder.connection_ids[-1]
        await context.server.stop()
        assert not context.server.server.server_state.tasks, (
            "server stop must join every accepted ASGI session task"
        )
        await context.recorder.wait_for_disconnect(conn_id)

    context.event_loop.run_until_complete(exercise())
    return context


@then("disconnect cleanup is recorded once with code 1001")
def then_cancellation_cleanup(context: LiveScenario) -> None:
    """Verify task cancellation is reported as going away exactly once."""
    context.event_loop.run_until_complete(stop_server(context))
    records = context.recorder.disconnect_records
    assert len(records) == 1, "cancelled accepted sessions must clean up once"
    assert context.recorder.disconnect_invocations == 1, (
        "cancellation must invoke on_disconnect exactly once"
    )
    assert records[0][1] == 1001, "cancellation cleanup must use code 1001"
