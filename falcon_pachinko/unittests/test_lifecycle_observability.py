"""Host observers receive bounded lifecycle metrics and completed spans."""

from __future__ import annotations

import asyncio
import dataclasses as dc
import logging

import pytest

from falcon_pachinko import WebSocketRouter
from falcon_pachinko.observability import LifecycleObservationEvent
from falcon_pachinko.unittests._lifecycle_observer_fixtures import (
    _FailingObserver,
    _observed_router,
    _ObservedResource,
    _RecordingObserver,
)
from falcon_pachinko.unittests._session_fixtures import SessionSocket
from falcon_pachinko.unittests.helpers import DummyWS, make_req

_EXPECTED_OPERATIONS = (
    "acceptance",
    "cleanup",
    "finalization",
    "negotiation",
    "receive_termination",
    "session_start",
)


def _event(observer: _RecordingObserver, operation: str) -> LifecycleObservationEvent:
    """Select the single completed measurement for one lifecycle operation."""
    events = [event for event in observer.events if event.operation == operation]
    assert len(events) == 1, f"{operation} must be measured once per lifecycle"
    return events[0]


@pytest.mark.asyncio
async def test_success_observes_lifecycle_once_and_keeps_frames_silent() -> None:
    """A multi-frame session records six operations and three completed spans."""
    observer = _RecordingObserver()
    resource = _ObservedResource()
    router = _observed_router(resource, observer)
    await router.on_websocket(make_req("/session"), DummyWS(("first", "second")))

    assert tuple(sorted(observer.operation_counts)) == _EXPECTED_OPERATIONS, (
        "normal frame traffic must use only the six lifecycle operation names"
    )
    assert all(count == 1 for count in observer.operation_counts.values()), (
        "each lifecycle operation must produce one measurement"
    )
    assert observer.operation_counts.total() == 6, (
        "normal frame traffic must not add metric events"
    )
    assert resource.frames == ["first", "second"], "observation must preserve dispatch"
    assert {span.name for span in observer.spans} == {
        "falcon_pachinko.websocket.negotiation",
        "falcon_pachinko.websocket.session",
        "falcon_pachinko.websocket.finalization",
    }, "the adapter must receive only the three fixed lifecycle spans"
    assert all(len(span.results) == 1 for span in observer.spans), (
        "every span must finish exactly once"
    )
    assert all(span.finish_calls == 1 for span in observer.spans), (
        "duplicate completion attempts must remain visible despite adapter isolation"
    )
    assert all(event.outcome == "succeeded" for event in observer.events), (
        "successful lifecycle operations must report success"
    )
    assert all(event.duration_seconds >= 0 for event in observer.events), (
        "durations must use a nonnegative monotonic interval"
    )


@pytest.mark.asyncio
async def test_rejection_observes_no_accepted_session_work() -> None:
    """A rejection completes negotiation without acceptance or session spans."""
    observer = _RecordingObserver()
    resource = _ObservedResource(accepted=False)
    socket = DummyWS()
    await _observed_router(resource, observer).on_websocket(
        make_req("/session"), socket
    )

    assert [(event.operation, event.outcome) for event in observer.events] == [
        ("negotiation", "rejected")
    ], "rejection must remain distinct from lifecycle failure"
    assert not socket.accept_calls, "observing rejection must not accept the socket"
    assert not resource.disconnects, "unaccepted connections have no disconnect hook"
    assert len(observer.spans) == 1, "rejection must create only the negotiation span"
    assert len(observer.spans[0].results) == 1, "the negotiation span must finish"


@pytest.mark.asyncio
async def test_setup_failure_finishes_spans_and_omits_sensitive_values() -> None:
    """A callback error remains primary and carries only bounded failure labels."""
    failure = RuntimeError("secret credential and payload")
    observer = _RecordingObserver()
    resource = _ObservedResource(start_error=failure)
    with pytest.raises(RuntimeError, match="secret credential") as raised:
        await _observed_router(resource, observer).on_websocket(
            make_req("/session"), DummyWS()
        )

    assert raised.value is failure, (
        "telemetry must preserve the original callback error"
    )
    event = _event(observer, "session_start")
    assert (event.outcome, event.failure_category) == ("failed", "application"), (
        "setup errors need a bounded application failure category"
    )
    assert all(len(span.results) == 1 for span in observer.spans), (
        "failure must finish every opened span"
    )
    assert "secret" not in repr((observer.events, observer.spans)), (
        "metric and trace data must exclude exception values"
    )
    assert [field.name for field in dc.fields(LifecycleObservationEvent)] == [
        "operation",
        "outcome",
        "duration_seconds",
        "failure_category",
        "close_code_category",
    ], "the event shape must have no fields for application identifiers or payloads"


@pytest.mark.asyncio
async def test_receive_cancellation_is_observed_and_preserves_cleanup() -> None:
    """Cancellation finishes spans and propagates without sending a close frame."""
    observer = _RecordingObserver()
    resource = _ObservedResource()
    socket = SessionSocket(block_receive=True)
    task = asyncio.create_task(
        _observed_router(resource, observer).on_websocket(make_req("/session"), socket)
    )
    await socket.receive_started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    event = _event(observer, "receive_termination")
    assert (event.outcome, event.failure_category) == ("cancelled", "cancelled"), (
        "receive termination must distinguish cancellation"
    )
    assert not socket.close_codes, "cancellation must not send reserved close code 1006"
    assert resource.disconnects == [1006], "cancellation cleanup must run exactly once"
    assert all(len(span.results) == 1 for span in observer.spans), (
        "cancellation must complete every started span"
    )


@pytest.mark.asyncio
async def test_returned_cleanup_failure_is_observed_as_failure(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Cleanup errors returned as values must not produce false success metrics."""
    failure = RuntimeError("secret cleanup failure")
    observer = _RecordingObserver()
    resource = _ObservedResource(cleanup_error=failure)
    caplog.set_level(logging.DEBUG, logger="falcon_pachinko._session_cleanup")
    with pytest.raises(RuntimeError, match="secret cleanup failure") as raised:
        await _observed_router(resource, observer).on_websocket(
            make_req("/session"), DummyWS()
        )

    assert raised.value is failure, "observation must preserve cleanup error precedence"
    event = _event(observer, "cleanup")
    assert (event.outcome, event.failure_category) == ("failed", "cleanup"), (
        "returned hook failures must be reflected in the cleanup measurement"
    )
    completions = [
        vars(record)["outcome"]
        for record in caplog.records
        if getattr(record, "operation", None) == "disconnect"
        and vars(record)["outcome"] != "started"
    ]
    assert completions == ["failed"], (
        "DEBUG cleanup observations must agree with metrics"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("code", "category"),
    [
        (1000, "normal"),
        (1001, "going_away"),
        (1002, "protocol_error"),
        (1011, "server_error"),
        (1006, "abnormal"),
        (4401, "other"),
    ],
)
async def test_peer_close_codes_use_finite_categories(code: int, category: str) -> None:
    """Custom close codes remain observable without becoming metric dimensions."""
    observer = _RecordingObserver()
    await _observed_router(_ObservedResource(), observer).on_websocket(
        make_req("/session"), DummyWS(disconnect_code=code)
    )
    event = _event(observer, "receive_termination")
    assert event.close_code_category == category, "peer codes must use fixed categories"
    assert "close_code" not in dc.asdict(event), (
        "events must expose no raw close-code field"
    )


@pytest.mark.asyncio
async def test_observer_can_be_disabled_without_changing_constructor_calls() -> None:
    """Disabling an installed adapter retains normal router behavior."""
    observer = _RecordingObserver()
    resource = _ObservedResource()
    router = _observed_router(resource, observer)
    router.lifecycle_observer = None
    await router.on_websocket(make_req("/session"), DummyWS(("frame",)))
    assert not observer.events, "disabled observers receive no metric calls"
    assert not observer.spans, "disabled observers receive no tracing calls"
    assert resource.frames == ["frame"], "disabled observation must preserve dispatch"
    assert resource.disconnects == [1000], "disabled observation must preserve cleanup"


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["start", "record", "finish"])
@pytest.mark.parametrize(
    "adapter_error",
    [
        pytest.param(RuntimeError("host sink failed"), id="exception"),
        pytest.param(asyncio.CancelledError("host sink cancelled"), id="cancellation"),
    ],
)
async def test_adapter_failures_cannot_replace_application_failure(
    stage: str,
    adapter_error: BaseException,
) -> None:
    """Metrics and tracing callbacks cannot mask application errors or cleanup."""
    failure = ValueError("application setup failed")
    observer = _FailingObserver(stage=stage, error=adapter_error)
    resource = _ObservedResource(start_error=failure)
    with pytest.raises(ValueError, match="application setup failed") as raised:
        await _observed_router(resource, observer).on_websocket(
            make_req("/session"), DummyWS()
        )
    assert raised.value is failure, (
        "host adapter failures must not change error identity"
    )
    assert resource.disconnects == [1011], "host failures must not interrupt cleanup"


@pytest.mark.asyncio
async def test_connection_keeps_observer_selected_before_resource_hooks() -> None:
    """Changing router configuration cannot split one connection's observations."""
    original = _RecordingObserver()
    replacement = _RecordingObserver()
    router = _observed_router(_ObservedResource(), original)

    def replace_observer(_context: object) -> None:
        """Simulate host reconfiguration after this connection took its snapshot."""
        router.lifecycle_observer = replacement

    router.global_hooks.add("before_connect", replace_observer)
    await router.on_websocket(make_req("/session"), DummyWS())
    assert len(original.events) == 6, "all operations must use the original adapter"
    assert len(original.spans) == 3, "all spans must use the original adapter"
    assert not replacement.events, "replacement metrics apply only to later connections"
    assert not replacement.spans, (
        "replacement tracing applies only to later connections"
    )


@pytest.mark.asyncio
async def test_transport_acceptance_failure_is_bounded_and_never_runs_session() -> None:
    """A failed handshake completes negotiation and acceptance observations."""
    failure = RuntimeError("private handshake failure")

    class FailingAcceptance(DummyWS):
        async def accept(
            self,
            subprotocol: str | None = None,
            headers: object = None,
        ) -> None:
            """Fail the transport handshake before its accepted transition."""
            del subprotocol, headers
            raise failure

    observer = _RecordingObserver()
    resource = _ObservedResource()
    with pytest.raises(RuntimeError, match="private handshake failure") as raised:
        await _observed_router(resource, observer).on_websocket(
            make_req("/session"), FailingAcceptance()
        )
    assert raised.value is failure, "transport failures must retain their identity"
    event = _event(observer, "acceptance")
    assert (event.outcome, event.failure_category) == ("failed", "transport"), (
        "handshake failures need a fixed transport category"
    )
    assert not resource.disconnects, (
        "a failed unaccepted handshake has no session cleanup"
    )
    assert all(len(span.results) == 1 for span in observer.spans), (
        "negotiation tracing must finish after a failed acceptance"
    )


@pytest.mark.asyncio
async def test_resource_construction_failure_finishes_negotiation_observation() -> None:
    """Negotiation includes resource resolution before the acceptance decision."""
    failure = RuntimeError("private resource construction failure")

    def fail_factory(_factory: object) -> _ObservedResource:
        """Fail resource construction before any accepted-session callbacks."""
        raise failure

    observer = _RecordingObserver()
    router = WebSocketRouter(resource_factory=fail_factory)
    router.lifecycle_observer = observer
    router.add_route("/session", _ObservedResource)
    router.mount("/")
    with pytest.raises(RuntimeError, match="private resource construction") as raised:
        await router.on_websocket(make_req("/session"), DummyWS())
    assert raised.value is failure, "resource factory failures must remain primary"
    assert [(event.operation, event.outcome) for event in observer.events] == [
        ("negotiation", "failed")
    ], "factory errors must complete negotiation without any acceptance event"
    assert len(observer.spans) == 1, (
        "factory failure must create only negotiation tracing"
    )
    assert len(observer.spans[0].results) == 1, (
        "factory failure must finish negotiation"
    )
