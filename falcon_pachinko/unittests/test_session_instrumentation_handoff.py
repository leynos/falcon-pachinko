"""Setup-only instrumentation outcomes and router configuration ownership."""

from __future__ import annotations

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

_ASSERTION_FAILURE = "router instrumentation must preserve bounded lifecycle semantics"


@pytest.mark.asyncio
async def test_setup_only_handoff_counts_outcome_without_claiming_session_end() -> None:
    """A setup-only router reports transfer instead of a completed session."""
    clock = InjectedClock()
    resource = RecordingResource()
    instrumentation = RecordingInstrumentation()
    router = configured_router(resource, instrumentation, clock, receive_loop=False)

    await drive_router(router, SessionSocket())

    _, amount, dimensions = session_counter(instrumentation)
    assert amount == 1, _ASSERTION_FAILURE
    assert dimensions.outcome is Outcome.HANDED_OFF, _ASSERTION_FAILURE
    assert not any(
        measurement is Duration.SESSION
        for measurement, _, _ in instrumentation.durations
    ), _ASSERTION_FAILURE
    assert [span.name for span in instrumentation.spans] == [SpanName.SESSION], (
        _ASSERTION_FAILURE
    )
    assert not resource.disconnect_codes, _ASSERTION_FAILURE
    assert instrumentation.spans[0].end_count == 1, _ASSERTION_FAILURE


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


@pytest.mark.asyncio
async def test_rejected_attempt_has_one_rejected_outcome_and_no_cleanup_span() -> None:
    """A rejected handshake is counted without claiming an accepted session."""
    clock = InjectedClock()
    resource = RecordingResource(accepted=False)
    instrumentation = RecordingInstrumentation()
    router = configured_router(resource, instrumentation, clock)

    await drive_router(router, SessionSocket())

    _, amount, dimensions = session_counter(instrumentation)
    assert amount == 1, _ASSERTION_FAILURE
    assert dimensions.outcome is Outcome.REJECTED, _ASSERTION_FAILURE
    assert not any(
        measurement is Duration.SESSION
        for measurement, _, _ in instrumentation.durations
    ), _ASSERTION_FAILURE
    assert [span.name for span in instrumentation.spans] == [SpanName.SESSION], (
        _ASSERTION_FAILURE
    )
    assert [span.end_count for span in instrumentation.spans] == [1], _ASSERTION_FAILURE
    assert not resource.disconnect_codes, _ASSERTION_FAILURE
