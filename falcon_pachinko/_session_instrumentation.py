"""Instrument bounded outcomes for one routed WebSocket session."""

from __future__ import annotations

import asyncio
import typing as typ

import falcon

from . import observability as _observability
from ._disconnect import _PeerDisconnectedError
from ._session import SessionPhase

if typ.TYPE_CHECKING:
    from ._session_driver import _SessionExecution, _SessionOptions, _SessionRun


class _SessionDriver(typ.Protocol):
    """Inject session work without coupling telemetry to the driver module."""

    async def __call__(
        self,
        execution: _SessionExecution,
        options: _SessionOptions,
        started: float,
    ) -> bool: ...


class _SessionReleaser(typ.Protocol):
    """Inject ownership release while keeping telemetry finalization central."""

    def __call__(
        self,
        execution: _SessionExecution,
        options: _SessionOptions,
        *,
        handed_off_to_application: bool,
    ) -> None: ...


async def _drive_session_with_observation(
    run: _SessionRun,
    drive_session: _SessionDriver,
) -> None:
    """Retain classification data while preserving the original traceback."""
    try:
        run.handed_off_to_application = await drive_session(
            run.execution, run.options, run.started
        )
    except BaseException as exc:
        run.error = exc
        run.outcome = _instrumentation_session_outcome(
            run.execution,
            exc,
            handed_off_to_application=run.handed_off_to_application,
        )
        raise
    run.outcome = _instrumentation_session_outcome(
        run.execution,
        None,
        handed_off_to_application=run.handed_off_to_application,
    )


def _release_and_observe_session(
    run: _SessionRun,
    release_session: _SessionReleaser,
) -> None:
    """Release session ownership before emitting its one terminal observation."""
    try:
        release_session(
            run.execution,
            run.options,
            handed_off_to_application=run.handed_off_to_application,
        )
    except BaseException as exc:
        run.error = exc
        run.outcome = _instrumentation_session_outcome(
            run.execution,
            exc,
            handed_off_to_application=run.handed_off_to_application,
        )
        raise
    finally:
        _finish_session_instrumentation(run)


def _start_session_span(
    execution: _SessionExecution, options: _SessionOptions
) -> _observability._SpanOwner:
    """Create the session span before setup and receive work begin."""
    return _observability._start_span(
        options.instrumentation,
        _observability.SpanName.SESSION,
        _instrumentation_dimensions(
            execution,
            operation=_observability.Operation.SESSION,
            outcome=_observability.Outcome.STARTED,
            exception=None,
        ),
    )


def _finish_session_instrumentation(run: _SessionRun) -> None:
    """Count one attempt outcome and end its span after ownership is released."""
    execution = run.execution
    outcome = run.outcome or _instrumentation_session_outcome(
        execution,
        run.error,
        handed_off_to_application=run.handed_off_to_application,
    )
    dimensions = _instrumentation_dimensions(
        execution,
        operation=_observability.Operation.SESSION,
        outcome=outcome,
        exception=_instrumentation_session_exception(execution, run.error),
    )
    session_duration = (
        run.options.instrumentation_clock() - run.instrumentation_started
        if execution.session.has_established and not run.handed_off_to_application
        else None
    )
    _observability._increment(
        run.options.instrumentation,
        _observability.Counter.SESSION_OUTCOMES,
        dimensions,
    )
    if session_duration is not None:
        _observability._observe_duration(
            run.options.instrumentation,
            _observability.Duration.SESSION,
            session_duration,
            dimensions,
        )
    run.session_span.end(dimensions)


def _instrumentation_session_outcome(
    execution: _SessionExecution,
    error: BaseException | None,
    *,
    handed_off_to_application: bool,
) -> _observability.Outcome:
    """Classify one router attempt without treating handoff as session end."""
    if error is not None:
        return (
            _observability.Outcome.CANCELLED
            if isinstance(error, asyncio.CancelledError)
            else _observability.Outcome.FAILED
        )
    if execution.session.phase is SessionPhase.CONNECTING:
        return _observability.Outcome.REJECTED
    return (
        _observability.Outcome.HANDED_OFF
        if handed_off_to_application
        else _observability.Outcome.COMPLETED
    )


def _instrumentation_session_exception(
    execution: _SessionExecution,
    session_error: BaseException | None,
) -> BaseException | None:
    """Prefer the original cause over grouped cleanup failures for dimensions."""
    if execution.primary_error is not None:
        return execution.primary_error
    if execution.cleanup_failures:
        return execution.cleanup_failures[0]
    return session_error


def _instrumentation_dimensions(
    execution: _SessionExecution,
    *,
    operation: _observability.Operation,
    outcome: _observability.Outcome,
    exception: BaseException | None,
) -> _observability.SessionDimensions:
    """Build dimensions solely from fixed enums and effective close metadata."""
    phase = {
        SessionPhase.CONNECTING: _observability.Phase.CONNECTING,
        SessionPhase.ESTABLISHED: _observability.Phase.ESTABLISHED,
        SessionPhase.DISCONNECTING: _observability.Phase.DISCONNECTING,
        SessionPhase.CLOSED: _observability.Phase.CLOSED,
    }[execution.session.phase]
    close_code = execution.session.close_code
    return _observability.SessionDimensions(
        operation=operation,
        outcome=outcome,
        phase=phase,
        close_code=_observability._close_code_category(close_code),
        exception=_observability._exception_category(
            exception,
            is_transport=isinstance(
                exception, (falcon.WebSocketDisconnected, _PeerDisconnectedError)
            ),
        ),
    )
