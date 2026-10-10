"""Live-socket BDD coverage for Falcon's WebSocket responder path."""

from __future__ import annotations

import dataclasses as dc
import typing as typ
from contextlib import AsyncExitStack

import pytest
from pytest_bdd import given, scenario, then, when
from websockets.exceptions import ConnectionClosed

from tests.behaviour._live_app import LiveAppState, build_live_app

if typ.TYPE_CHECKING:
    from contextlib import AbstractAsyncContextManager

    from falcon_pachinko.testing import LiveWebSocketServer
    from falcon_pachinko.testing.client import WebSocketSession
    from falcon_pachinko.testing.fixtures import _LiveServerRunner


_MISSING_SESSION_MSG = "the client must open a WebSocket session first"


@dc.dataclass(slots=True)
class LiveScenario:
    """Keep the live app and real client session available to BDD steps."""

    runner: _LiveServerRunner
    server: LiveWebSocketServer
    app_state: LiveAppState
    connection_stack: AsyncExitStack | None = None
    session: WebSocketSession | None = None
    welcome_frame: object | None = None
    acknowledgement: object | None = None
    echo_frames: list[object] = dc.field(default_factory=list)
    client_close_code: int | None = None


def _session(context: LiveScenario) -> WebSocketSession:
    """Return the opened client session or fail with a useful assertion."""
    if context.session is None:
        raise AssertionError(_MISSING_SESSION_MSG)
    return context.session


def _close_connection(context: LiveScenario) -> None:
    """Exit the client context before the live-server fixture tears down."""
    connection_stack = context.connection_stack
    if connection_stack is not None:
        context.runner.run(connection_stack.aclose(), timeout=2.0)


async def _enter_connection(
    stack: AsyncExitStack,
    connection_context: AbstractAsyncContextManager[WebSocketSession],
) -> WebSocketSession:
    """Enter a client session on the fixture's event loop and register cleanup."""
    return await stack.enter_async_context(connection_context)


@scenario(
    "live_websocket_server.feature",
    "client opens a WebSocket through Falcon",
)
def test_live_websocket_connection_opens() -> (
    None
):  # pragma: no cover - BDD registration
    """Register the connection-open acceptance scenario."""


@scenario(
    "live_websocket_server.feature",
    "server sends a frame to the client",
)
def test_live_websocket_server_sends_frame() -> (
    None
):  # pragma: no cover - BDD registration
    """Register the server-to-client frame acceptance scenario."""


@scenario(
    "live_websocket_server.feature",
    "client frames reach application code",
)
def test_live_websocket_client_frame_reaches_app() -> (
    None
):  # pragma: no cover - BDD registration
    """Register the client-to-application frame acceptance scenario."""


@scenario(
    "live_websocket_server.feature",
    "multiple frames traverse one connection",
)
def test_live_websocket_multiple_frames() -> (
    None
):  # pragma: no cover - BDD registration
    """Register the persistent multi-frame acceptance scenario."""


@scenario(
    "live_websocket_server.feature",
    "clean client disconnect tears down the server session",
)
def test_live_websocket_clean_disconnect() -> (
    None
):  # pragma: no cover - BDD registration
    """Register the clean disconnect acceptance scenario."""


@scenario(
    "live_websocket_server.feature",
    "server failure is surfaced by the live harness",
)
def test_live_websocket_server_failure_is_surfaced() -> (
    None
):  # pragma: no cover - BDD registration
    """Register the server-error acceptance scenario."""


@given(
    "a Falcon ASGI app mounted with the live WebSocket router",
    target_fixture="context",
)
def given_live_falcon_app(
    live_websocket_server: _LiveServerRunner,
    request: pytest.FixtureRequest,
) -> LiveScenario:
    """Start a real Falcon app and arrange per-scenario client cleanup."""
    app_state = LiveAppState()
    server = live_websocket_server.start(build_live_app(app_state))
    context = LiveScenario(
        runner=live_websocket_server,
        server=server,
        app_state=app_state,
    )
    request.addfinalizer(lambda: _close_connection(context))
    return context


@when('the client opens "/ws/socket"', target_fixture="context")
def when_client_opens_connection(context: LiveScenario) -> LiveScenario:
    """Open a real RFC WebSocket and consume the server's welcome frame."""
    connection_stack = AsyncExitStack()
    context.connection_stack = connection_stack
    context.session = context.runner.run(
        _enter_connection(connection_stack, context.server.connect("/ws/socket")),
        timeout=3.0,
    )
    context.welcome_frame = context.runner.run(
        _session(context).receive_json(), timeout=2.0
    )
    return context


@when('the client records "from the client"', target_fixture="context")
def when_client_sends_record_frame(context: LiveScenario) -> LiveScenario:
    """Send a real client frame and wait for application-visible state."""
    session = _session(context)
    context.runner.run(
        session.send_json({"type": "record", "payload": "from the client"}),
        timeout=2.0,
    )
    context.runner.run(context.app_state.recorded_event.wait(), timeout=2.0)
    context.acknowledgement = context.runner.run(session.receive_json(), timeout=2.0)
    return context


@when("the client exchanges two echo frames", target_fixture="context")
def when_client_exchanges_echo_frames(context: LiveScenario) -> LiveScenario:
    """Send and receive two frames over the already-open client session."""
    session = _session(context)
    for payload in ("first", "second"):
        context.runner.run(
            session.send_json({"type": "echo", "payload": payload}), timeout=2.0
        )
    for _ in range(2):
        context.echo_frames.append(
            context.runner.run(session.receive_json(), timeout=2.0)
        )
    return context


@when("the client disconnects cleanly", target_fixture="context")
def when_client_disconnects_cleanly(context: LiveScenario) -> LiveScenario:
    """Close the client normally and wait for app cleanup to run."""
    context.runner.run(_session(context).close(code=1000), timeout=2.0)
    context.runner.run(context.app_state.disconnected.wait(), timeout=2.0)
    return context


@when("the client triggers a server failure", target_fixture="context")
def when_client_triggers_server_failure(context: LiveScenario) -> LiveScenario:
    """Exercise the failing responder through the live client connection."""
    session = _session(context)
    context.runner.run(
        session.send_json({"type": "explode", "payload": "fail"}), timeout=2.0
    )
    try:
        context.runner.run(session.receive_text(), timeout=2.0)
    except ConnectionClosed as exc:
        context.client_close_code = exc.code
    else:
        pytest.fail("the failing responder must close the client connection")
    context.runner.run(context.app_state.disconnected.wait(), timeout=2.0)
    return context


@then("the WebSocket connection is open")
def then_connection_is_open(context: LiveScenario) -> None:
    """Confirm the client completed Falcon's real WebSocket handshake."""
    assert not _session(context).closed, "the live connection must remain open"


@then("the client receives the welcome frame")
def then_client_receives_welcome(context: LiveScenario) -> None:
    """Check that Falcon delivered the frame sent during resource setup."""
    assert context.welcome_frame == {
        "type": "welcome",
        "message": "welcome from Falcon",
    }, "the server must send its welcome frame through Falcon"


@then("the application records the client frame")
def then_application_records_client_frame(context: LiveScenario) -> None:
    """Verify both application state and its acknowledgement frame."""
    assert context.app_state.received == ["from the client"], (
        "the client payload must reach application code"
    )
    assert context.acknowledgement == {
        "type": "recorded",
        "payload": "from the client",
    }, "the application must acknowledge the recorded payload"


@then("both frames return over the same connection")
def then_both_frames_return(context: LiveScenario) -> None:
    """Check both echo frames arrive without opening another connection."""
    assert context.echo_frames == [
        {"type": "echo", "payload": "first"},
        {"type": "echo", "payload": "second"},
    ], "the persistent connection must carry both echo frames"
    assert not _session(context).closed, (
        "the connection must remain open after both frames"
    )


@then("the server session closes with code 1000")
def then_server_session_closes_cleanly(context: LiveScenario) -> None:
    """Assert normal peer closure reaches the resource's disconnect hook."""
    assert context.app_state.disconnected.is_set(), (
        "client disconnect must signal server-side cleanup"
    )
    assert context.app_state.disconnect_code == 1000, (
        "clean client disconnect must preserve close code 1000"
    )


@then("the client observes close code 1011")
def then_client_observes_internal_error_close(context: LiveScenario) -> None:
    """Check the RFC client observes the server's internal-error close."""
    assert context.client_close_code == 1011, (
        "a responder failure must close the real client with code 1011"
    )
    assert context.app_state.disconnect_code == 1011, (
        "the resource cleanup hook must receive the responder failure code"
    )


@then("the harness exposes the server exception")
def then_harness_exposes_server_exception(context: LiveScenario) -> None:
    """Assert the error snapshot and acknowledge the scenario failure."""
    errors = context.server.errors
    assert len(errors) == 1, "the harness must capture the responder failure"
    assert isinstance(errors[0], RuntimeError), (
        "the captured failure must retain its application exception type"
    )
    acknowledged = context.server.pop_errors()
    assert len(acknowledged) == 1, "the scenario must acknowledge one captured error"
    assert isinstance(acknowledged[0], RuntimeError), (
        "the scenario must acknowledge the captured RuntimeError"
    )
