"""Check live-server cleanup and captured-error invariants across generated inputs.

These Hypothesis tests complement example-based lifecycle tests by exploring
bounded combinations of session outcomes and error acknowledgements. Run them
with ``make test`` or by selecting this module with pytest.
"""

from __future__ import annotations

import asyncio
import typing as typ

import falcon.asgi
from hypothesis import given
from hypothesis import strategies as st

from falcon_pachinko.testing import LiveWebSocketServer

if typ.TYPE_CHECKING:
    from falcon_pachinko.testing.client import WebSocketSession


class _CloseSession:
    """Model one generated outcome from a client close handshake."""

    def __init__(self, outcome: str, index: int) -> None:
        self.outcome = outcome
        self.index = index
        self.closed = False
        self.close_calls = 0

    async def close(self) -> None:
        self.close_calls += 1
        match self.outcome:
            case "timeout":
                await asyncio.Future()
            case "error":
                msg = f"session {self.index} close failed"
                raise RuntimeError(msg)
            case "success":
                self.closed = True


def _expected_close_results(
    outcomes: list[str],
) -> tuple[list[tuple[str, str]], list[bool]]:
    """Return expected errors and closed flags for generated outcomes."""
    expected_errors: list[tuple[str, str]] = []
    expected_closed: list[bool] = []

    for index, outcome in enumerate(outcomes):
        match outcome:
            case "timeout":
                expected_closed.append(False)
                expected_errors.append((
                    "LiveServerShutdownError",
                    "WebSocket session shutdown timed out",
                ))
            case "error":
                expected_closed.append(False)
                expected_errors.append((
                    "RuntimeError",
                    f"session {index} close failed",
                ))
            case "success":
                expected_closed.append(True)

    return expected_errors, expected_closed


@given(outcomes=st.lists(st.sampled_from(("success", "error", "timeout")), max_size=8))
def test_close_sessions_attempts_and_records_generated_outcomes(
    outcomes: list[str],
) -> None:
    """Attempt each generated close once and retain every generated failure."""
    sessions = [_CloseSession(outcome, index) for index, outcome in enumerate(outcomes)]
    expected_errors, expected_closed = _expected_close_results(outcomes)

    async def close_sessions() -> LiveWebSocketServer:
        """Run cleanup with its required asyncio timeout context.

        Inject generated sessions because ``connect()`` only provides real
        client sessions and cannot exercise each close outcome deterministically.

        Returns
        -------
        LiveWebSocketServer
            The server after all generated sessions have been closed.
        """
        server = LiveWebSocketServer(falcon.asgi.App(), shutdown_timeout=0.001)
        server._sessions.update(
            typ.cast("WebSocketSession", session) for session in sessions
        )
        await server._close_sessions()
        return server

    server = asyncio.run(close_sessions())

    assert all(session.close_calls == 1 for session in sessions), (
        "every generated session must be attempted exactly once"
    )
    assert not server._sessions, "every attempted session must leave tracking"
    assert sorted(
        (type(error).__name__, str(error)) for error in server.errors
    ) == sorted(expected_errors), (
        "cleanup must retain exactly the generated close failures"
    )
    assert [session.closed for session in sessions] == expected_closed, (
        "only successful sessions must be marked closed"
    )


@given(operations=st.lists(st.sampled_from(("record", "acknowledge")), max_size=20))
def test_error_recording_and_acknowledgement_follow_generated_operations(
    operations: list[str],
) -> None:
    """Keep only failures recorded since the latest acknowledgement."""
    server = LiveWebSocketServer(falcon.asgi.App())
    pending: list[Exception] = []
    next_error = 0

    for operation in operations:
        match operation:
            case "record":
                failure = RuntimeError(f"failure {next_error}")
                next_error += 1
                server.record_error(failure)
                pending.append(failure)
            case "acknowledge":
                assert server.pop_errors() == tuple(pending), (
                    "acknowledgement must return exactly the pending failures"
                )
                pending.clear()

    assert server.errors == tuple(pending), (
        "only failures recorded after the last acknowledgement may remain"
    )
