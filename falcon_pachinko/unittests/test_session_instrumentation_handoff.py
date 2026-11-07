"""Setup-only instrumentation outcomes and router configuration ownership."""

from __future__ import annotations

import typing as typ

import pytest

from falcon_pachinko.observability import Duration, Outcome, SpanName
from falcon_pachinko.unittests._session_fixtures import RecordingResource, SessionSocket
from falcon_pachinko.unittests.test_session_instrumentation_fixtures import (
    InjectedClock,
    RecordingInstrumentation,
    configured_router,
    drive_router,
    session_counter,
)

if typ.TYPE_CHECKING:
    from falcon_pachinko.protocols import WebSocketLike

_ASSERTION_FAILURE = "router instrumentation must preserve bounded lifecycle semantics"


def _assert_setup_only_outcome(
    instrumentation: RecordingInstrumentation, expected_outcome: Outcome
) -> None:
    """Assert a terminal setup outcome without a completed session duration."""
    _, amount, dimensions = session_counter(instrumentation)
    assert amount == 1, _ASSERTION_FAILURE
    assert dimensions.outcome is expected_outcome, _ASSERTION_FAILURE
    assert not any(
        measurement is Duration.SESSION
        for measurement, _, _ in instrumentation.durations
    ), _ASSERTION_FAILURE
    assert [span.name for span in instrumentation.spans] == [SpanName.SESSION], (
        _ASSERTION_FAILURE
    )
    assert [span.end_count for span in instrumentation.spans] == [1], _ASSERTION_FAILURE


class StartingResource(RecordingResource):
    """Record startup for the automatic application-owned handoff path."""

    def __init__(self, *, startup_error: Exception | None = None) -> None:
        """Store the optional startup error and start with no recorded calls."""
        super().__init__()
        self.startup_error = startup_error
        self.start_calls = 0
        self.started_socket: WebSocketLike | None = None

    async def on_session_start(self, ws: WebSocketLike) -> None:
        """Capture startup and optionally fail before ownership transfers."""
        self.start_calls += 1
        self.started_socket = ws
        if self.startup_error is not None:
            raise self.startup_error


@pytest.mark.asyncio
async def test_application_handoff_runs_start_and_releases_router_dispatch_guard() -> (
    None
):
    """Automatic setup completes before application-owned receive dispatch."""
    resource = StartingResource()
    router = configured_router(
        resource, RecordingInstrumentation(), InjectedClock(), receive_loop=False
    )
    socket = SessionSocket()

    await drive_router(router, socket)

    assert resource.start_calls == 1, "handoff must run post-accept startup once"
    assert resource.started_socket is not None, "startup receives the guarded socket"
    assert not socket.receive_started.is_set(), "handoff must not start router receive"
    assert not socket.closed, "successful handoff keeps the accepted socket open"
    assert not resource.has_router_session, (
        "application ownership releases router state"
    )
    await resource.dispatch(resource.started_socket, '{"type":"unknown"}')
    assert resource.handled_frames == ['{"type":"unknown"}'], (
        "application dispatch remains enabled after handoff"
    )
    assert not resource.disconnect_codes, "application ownership skips router cleanup"


@pytest.mark.asyncio
async def test_application_handoff_start_failure_uses_router_cleanup() -> None:
    """Failed post-accept setup closes and finalizes instead of handing off."""
    failure = RuntimeError("startup failed")
    resource = StartingResource(startup_error=failure)
    instrumentation = RecordingInstrumentation()
    router = configured_router(
        resource, instrumentation, InjectedClock(), receive_loop=False
    )
    socket = SessionSocket()

    with pytest.raises(RuntimeError, match="startup failed") as caught:
        await drive_router(router, socket)

    _, amount, dimensions = session_counter(instrumentation)
    assert caught.value is failure, "startup failure identity must be preserved"
    assert amount == 1, "failed handoff has one session outcome"
    assert dimensions.outcome is Outcome.FAILED, "failed setup is not a handoff"
    assert resource.start_calls == 1, "startup is attempted exactly once"
    assert socket.close_codes == [1011], "startup failure closes with internal error"
    assert resource.disconnect_codes == [1011], "router cleanup runs exactly once"
    assert not resource.has_router_session, "failed setup releases router session state"


@pytest.mark.asyncio
async def test_instrumentation_configuration_freezes_after_first_routed_session() -> (
    None
):
    """Hosts cannot replace a provider while routed sessions may be active."""
    resource = RecordingResource()
    clock = InjectedClock()
    instrumentation = RecordingInstrumentation()
    router = configured_router(resource, instrumentation, clock, receive_loop=False)

    await drive_router(router, SessionSocket())

    with pytest.raises(RuntimeError, match="before first use"):
        router.configure_session_instrumentation(RecordingInstrumentation())


@pytest.mark.parametrize(
    ("scenario", "expected_outcome"),
    [
        ("handed-off", Outcome.HANDED_OFF),
        ("rejected", Outcome.REJECTED),
    ],
    ids=["handed-off", "rejected"],
)
@pytest.mark.asyncio
async def test_setup_outcomes_have_no_completed_session_duration(
    scenario: typ.Literal["handed-off", "rejected"],
    expected_outcome: Outcome,
) -> None:
    """Count handoff and rejection without recording completed durations."""
    clock = InjectedClock()
    resource = RecordingResource(accepted=scenario != "rejected")
    instrumentation = RecordingInstrumentation()
    router = configured_router(
        resource, instrumentation, clock, receive_loop=scenario != "handed-off"
    )

    await drive_router(router, SessionSocket())

    _assert_setup_only_outcome(instrumentation, expected_outcome)
    assert not resource.disconnect_codes, _ASSERTION_FAILURE
