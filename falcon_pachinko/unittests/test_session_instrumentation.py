"""Instrumentation contracts exercised through the production router."""

from __future__ import annotations

import asyncio
import typing as typ

import falcon
import pytest

from falcon_pachinko.observability import (
    CloseCodeCategory,
    Counter,
    Duration,
    ExceptionCategory,
    Operation,
    Outcome,
    Phase,
    SpanName,
)
from falcon_pachinko.unittests._session_fixtures import RecordingResource, SessionSocket
from falcon_pachinko.unittests.test_session_instrumentation_fixtures import (
    FailingInstrumentation,
    FailingSpanInstrumentation,
    InjectedClock,
    RecordingInstrumentation,
    assert_bounded_dimensions,
    configured_router,
    drive_router,
    session_counter,
)

_ASSERTION_FAILURE = "router instrumentation must preserve bounded lifecycle semantics"


@pytest.mark.asyncio
async def test_normal_close_records_once_with_exact_durations_and_child_span() -> None:
    """A normal close emits one outcome and a cleanup span under its session."""
    clock = InjectedClock()
    resource = RecordingResource()
    instrumentation = RecordingInstrumentation()

    async def on_connect(req: object, ws: object, **params: object) -> bool:
        del req, ws, params
        clock.value = 2.0
        await asyncio.sleep(0)
        return True

    async def on_disconnect(ws: object, close_code: int) -> None:
        del ws
        resource.disconnect_codes.append(close_code)
        clock.value = 7.0
        await asyncio.sleep(0)

    dynamic_resource = typ.cast("typ.Any", resource)
    dynamic_resource.on_connect = on_connect
    dynamic_resource.on_disconnect = on_disconnect
    router = configured_router(resource, instrumentation, clock)

    await drive_router(router, SessionSocket(falcon.WebSocketDisconnected(4000)))

    _, amount, session_dimensions = session_counter(instrumentation)
    assert amount == 1, _ASSERTION_FAILURE
    assert session_dimensions.outcome is Outcome.COMPLETED, _ASSERTION_FAILURE
    assert session_dimensions.close_code is CloseCodeCategory.APPLICATION, (
        _ASSERTION_FAILURE
    )
    assert [item[:2] for item in instrumentation.durations] == [
        (Duration.CLEANUP, 5.0),
        (Duration.SESSION, 7.0),
    ], _ASSERTION_FAILURE
    assert clock.readings == [0.0, 2.0, 7.0, 7.0], _ASSERTION_FAILURE
    assert [span.name for span in instrumentation.spans] == [
        SpanName.SESSION,
        SpanName.CLEANUP,
    ], _ASSERTION_FAILURE
    session_span, cleanup_span = instrumentation.spans
    assert cleanup_span.parent is session_span, _ASSERTION_FAILURE
    assert session_span.dimensions == session_dimensions, _ASSERTION_FAILURE
    assert cleanup_span.dimensions.outcome is Outcome.COMPLETED, _ASSERTION_FAILURE
    assert [span.end_count for span in instrumentation.spans] == [1, 1], (
        _ASSERTION_FAILURE
    )
    assert resource.disconnect_codes == [4000], _ASSERTION_FAILURE
    for _, _, dimensions in instrumentation.counters:
        assert_bounded_dimensions(dimensions)
    for _, _, dimensions in instrumentation.durations:
        assert_bounded_dimensions(dimensions)


@pytest.mark.asyncio
async def test_transport_loss_is_a_completed_outcome_with_abnormal_close_category() -> (
    None
):
    """Transport loss is not an application exception and uses category only."""
    clock = InjectedClock()
    resource = RecordingResource()
    instrumentation = RecordingInstrumentation()
    router = configured_router(resource, instrumentation, clock)

    await drive_router(router, SessionSocket(falcon.WebSocketDisconnected(1006)))

    _, _, dimensions = session_counter(instrumentation)
    assert dimensions.outcome is Outcome.COMPLETED, _ASSERTION_FAILURE
    assert dimensions.close_code is CloseCodeCategory.ABNORMAL, _ASSERTION_FAILURE
    assert dimensions.exception is ExceptionCategory.NONE, _ASSERTION_FAILURE
    assert resource.disconnect_codes == [1006], _ASSERTION_FAILURE
    assert [span.end_count for span in instrumentation.spans] == [1, 1], (
        _ASSERTION_FAILURE
    )


@pytest.mark.asyncio
async def test_cancellation_records_going_away_and_finishes_both_spans() -> None:
    """Task cancellation remains cancellation while cleanup telemetry completes."""
    clock = InjectedClock()
    resource = RecordingResource()
    instrumentation = RecordingInstrumentation()
    router = configured_router(resource, instrumentation, clock)
    socket = SessionSocket(block_receive=True)
    task = asyncio.create_task(drive_router(router, socket))
    await socket.receive_started.wait()

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    _, _, dimensions = session_counter(instrumentation)
    assert dimensions.outcome is Outcome.CANCELLED, _ASSERTION_FAILURE
    assert dimensions.close_code is CloseCodeCategory.GOING_AWAY, _ASSERTION_FAILURE
    assert dimensions.exception is ExceptionCategory.CANCELLATION, _ASSERTION_FAILURE
    assert resource.disconnect_codes == [1001], _ASSERTION_FAILURE
    assert [span.end_count for span in instrumentation.spans] == [1, 1], (
        _ASSERTION_FAILURE
    )


@pytest.mark.asyncio
async def test_repeated_cancellation_during_cleanup_ends_child_span_once() -> None:
    """Repeated cancellation joins the same cleanup span to completion."""
    clock = InjectedClock()
    resource = RecordingResource()
    resource.wait_in_disconnect = True
    instrumentation = RecordingInstrumentation()
    router = configured_router(resource, instrumentation, clock)
    task = asyncio.create_task(
        drive_router(router, SessionSocket(falcon.WebSocketDisconnected(4000)))
    )
    await resource.disconnect_started.wait()

    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done(), _ASSERTION_FAILURE
    clock.value = 8.0
    resource.finish_disconnect.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    session_span, cleanup_span = instrumentation.spans
    assert cleanup_span.parent is session_span, _ASSERTION_FAILURE
    assert [span.end_count for span in instrumentation.spans] == [1, 1], (
        _ASSERTION_FAILURE
    )
    _, _, dimensions = session_counter(instrumentation)
    assert dimensions.outcome is Outcome.CANCELLED, _ASSERTION_FAILURE
    assert dimensions.close_code is CloseCodeCategory.APPLICATION, _ASSERTION_FAILURE
    assert session_span.dimensions.outcome is Outcome.CANCELLED, _ASSERTION_FAILURE
    assert cleanup_span.dimensions.outcome is Outcome.COMPLETED, _ASSERTION_FAILURE
    assert cleanup_span.dimensions.phase is Phase.CLOSED, _ASSERTION_FAILURE
    assert [item[:2] for item in instrumentation.durations] == [
        (Duration.CLEANUP, 8.0),
        (Duration.SESSION, 8.0),
    ], _ASSERTION_FAILURE
    assert resource.disconnect_codes == [4000], _ASSERTION_FAILURE


@pytest.mark.asyncio
async def test_handler_failure_records_bounded_exception_category() -> None:
    """A handler exception uses fixed dimensions and retains its identity."""

    class SecretFailureError(RuntimeError):
        pass

    failure = SecretFailureError("payload and connection ID must not become labels")
    clock = InjectedClock()
    resource = RecordingResource()
    resource.dispatch_error = failure
    instrumentation = RecordingInstrumentation()
    router = configured_router(resource, instrumentation, clock)

    with pytest.raises(SecretFailureError) as raised:
        await drive_router(router, SessionSocket(b"sensitive frame"))

    assert raised.value is failure, _ASSERTION_FAILURE
    _, _, dimensions = session_counter(instrumentation)
    assert dimensions.outcome is Outcome.FAILED, _ASSERTION_FAILURE
    assert dimensions.exception is ExceptionCategory.ORDINARY, _ASSERTION_FAILURE
    assert "SecretFailureError" not in repr(dimensions), _ASSERTION_FAILURE
    assert "sensitive frame" not in repr(instrumentation.counters), _ASSERTION_FAILURE
    assert resource.disconnect_codes == [1011], _ASSERTION_FAILURE
    assert [span.end_count for span in instrumentation.spans] == [1, 1], (
        _ASSERTION_FAILURE
    )


@pytest.mark.asyncio
async def test_setup_failure_after_accept_records_session_and_cleanup_durations() -> (
    None
):
    """Acceptance followed by setup failure still measures promised cleanup."""
    failure = RuntimeError("setup failed")
    clock = InjectedClock()
    resource = RecordingResource()
    instrumentation = RecordingInstrumentation()

    async def on_connect(req: object, ws: object, **params: object) -> bool:
        del req, params
        await typ.cast("typ.Any", ws).accept()
        clock.value = 3.0
        await asyncio.sleep(0)
        raise failure

    async def on_disconnect(ws: object, close_code: int) -> None:
        del ws
        resource.disconnect_codes.append(close_code)
        clock.value = 9.0
        await asyncio.sleep(0)

    dynamic_resource = typ.cast("typ.Any", resource)
    dynamic_resource.on_connect = on_connect
    dynamic_resource.on_disconnect = on_disconnect
    router = configured_router(resource, instrumentation, clock)

    with pytest.raises(RuntimeError, match="setup failed") as raised:
        await drive_router(router, SessionSocket())

    assert raised.value is failure, _ASSERTION_FAILURE
    _, _, dimensions = session_counter(instrumentation)
    assert dimensions.outcome is Outcome.FAILED, _ASSERTION_FAILURE
    assert [item[:2] for item in instrumentation.durations] == [
        (Duration.CLEANUP, 6.0),
        (Duration.SESSION, 9.0),
    ], _ASSERTION_FAILURE
    assert resource.disconnect_codes == [1011], _ASSERTION_FAILURE
    assert [span.end_count for span in instrumentation.spans] == [1, 1], (
        _ASSERTION_FAILURE
    )


@pytest.mark.asyncio
async def test_cleanup_failure_is_counted_once_and_keeps_cleanup_span_complete() -> (
    None
):
    """A cleanup exception is measured without hiding its propagation."""
    failure = ValueError("cleanup failed")
    clock = InjectedClock()
    resource = RecordingResource()
    instrumentation = RecordingInstrumentation()

    async def on_disconnect(ws: object, close_code: int) -> None:
        del ws
        resource.disconnect_codes.append(close_code)
        clock.value = 4.0
        await asyncio.sleep(0)
        raise failure

    typ.cast("typ.Any", resource).on_disconnect = on_disconnect
    router = configured_router(resource, instrumentation, clock)

    with pytest.raises(ValueError, match="cleanup failed") as raised:
        await drive_router(router, SessionSocket(falcon.WebSocketDisconnected(4000)))

    assert raised.value is failure, _ASSERTION_FAILURE
    _, _, dimensions = session_counter(instrumentation)
    assert dimensions.outcome is Outcome.FAILED, _ASSERTION_FAILURE
    cleanup_failures = [
        event
        for event in instrumentation.counters
        if event[0] is Counter.CLEANUP_FAILURES
    ]
    assert len(cleanup_failures) == 1, _ASSERTION_FAILURE
    assert cleanup_failures[0][1] == 1, _ASSERTION_FAILURE
    assert cleanup_failures[0][2].operation is Operation.CLEANUP, _ASSERTION_FAILURE
    assert cleanup_failures[0][2].exception is ExceptionCategory.ORDINARY, (
        _ASSERTION_FAILURE
    )
    assert instrumentation.spans[1].dimensions.outcome is Outcome.FAILED, (
        _ASSERTION_FAILURE
    )
    assert [item[:2] for item in instrumentation.durations] == [
        (Duration.CLEANUP, 4.0),
        (Duration.SESSION, 4.0),
    ], _ASSERTION_FAILURE
    assert [span.end_count for span in instrumentation.spans] == [1, 1], (
        _ASSERTION_FAILURE
    )
    for _, _, event_dimensions in instrumentation.counters:
        assert_bounded_dimensions(event_dimensions)
    for _, _, event_dimensions in instrumentation.durations:
        assert_bounded_dimensions(event_dimensions)


@pytest.mark.asyncio
async def test_instrumentation_failures_do_not_cancel_or_replace_session_failure() -> (
    None
):
    """Broken telemetry cannot interrupt cleanup or alter failure precedence."""
    failure = RuntimeError("handler failure")
    clock = InjectedClock()
    resource = RecordingResource()
    resource.dispatch_error = failure
    router = configured_router(resource, FailingInstrumentation(), clock)

    with pytest.raises(RuntimeError) as raised:
        await drive_router(router, SessionSocket(b"frame"))

    assert raised.value is failure, _ASSERTION_FAILURE
    assert resource.disconnect_codes == [1011], _ASSERTION_FAILURE


@pytest.mark.asyncio
async def test_instrumentation_cancellation_does_not_replace_task_cancellation() -> (
    None
):
    """A callback-raised CancelledError cannot substitute for caller cancellation."""
    clock = InjectedClock()
    resource = RecordingResource()
    router = configured_router(resource, FailingInstrumentation(), clock)
    socket = SessionSocket(block_receive=True)
    task = asyncio.create_task(drive_router(router, socket))
    await socket.receive_started.wait()

    task.cancel()
    with pytest.raises(asyncio.CancelledError) as raised:
        await task

    assert str(raised.value) != "instrumentation cancellation", _ASSERTION_FAILURE
    assert resource.disconnect_codes == [1001], _ASSERTION_FAILURE


@pytest.mark.asyncio
async def test_span_finalization_failures_do_not_interrupt_cleanup() -> None:
    """Failing attribute and span-end callbacks cannot abort disconnect hooks."""
    clock = InjectedClock()
    resource = RecordingResource()
    instrumentation = FailingSpanInstrumentation()
    router = configured_router(resource, instrumentation, clock)

    await drive_router(router, SessionSocket(falcon.WebSocketDisconnected(4000)))

    assert resource.disconnect_codes == [4000], _ASSERTION_FAILURE
    assert [span.end_count for span in instrumentation.spans] == [1, 1], (
        _ASSERTION_FAILURE
    )
