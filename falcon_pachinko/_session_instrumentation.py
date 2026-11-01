"""Isolate host-configured telemetry around one routed WebSocket session.

The session driver uses this boundary to own session counters, durations, and
span completion without requiring a telemetry SDK. Its fixed dimensions and
failure isolation prevent host callback errors from changing session outcomes.
"""

from __future__ import annotations

import asyncio
import dataclasses as dc
import typing as typ

import falcon

from . import observability as _observability
from ._disconnect import _PeerDisconnectedError
from ._session import SessionPhase, SessionState
from ._session_observation import (
    _log_session_observation,
    _outcome_for_error,
    _SessionObservation,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from .observability import SessionInstrumentation


@dc.dataclass(slots=True)
class _CleanupInstrumentation:
    """Retain one cleanup span and its monotonic start time."""

    attempt: _SessionInstrumentationRun
    started: float
    span: _observability._SpanOwner


class _SessionInstrumentationRun:
    """Own bounded telemetry for one route attempt from negotiation to end."""

    def __init__(
        self,
        session: SessionState,
        instrumentation: SessionInstrumentation,
        clock: cabc.Callable[[], float],
    ) -> None:
        """Start the session span and retain its state for terminal reporting."""
        self.session = session
        self.instrumentation = instrumentation
        self.observation_clock = clock
        self.started = clock()
        self.setup_started = self.started
        self.resource: object = None
        self.cleanup_failures: tuple[BaseException, ...] = ()
        self._finished = False
        dimensions = _instrumentation_dimensions(
            session,
            operation=_observability.Operation.SESSION,
            outcome=_observability.Outcome.STARTED,
            error=None,
        )
        self.session_span = _observability._start_span(
            instrumentation, _observability.SpanName.SESSION, dimensions
        )
        _log_session_observation(
            self, _SessionObservation("session", "started", self.started)
        )
        self.setup_started = clock()
        _log_session_observation(
            self, _SessionObservation("setup", "started", self.setup_started)
        )

    @property
    def primary_error(self) -> BaseException | None:
        """Original session failure for bounded log metadata."""
        return self.session.primary_error

    def set_resource(self, resource: object) -> None:
        """Retain the selected resource for type-only diagnostics."""
        self.resource = resource

    def finish_setup(self, error: BaseException | None = None) -> None:
        """Log the negotiation/setup boundary without exposing caller values."""
        outcome = _outcome_for_error(error)
        if error is None and self.session.phase is SessionPhase.CONNECTING:
            outcome = "rejected"
        _log_session_observation(
            self,
            _SessionObservation("setup", outcome, self.setup_started, error=error),
        )

    def finish(
        self,
        error: BaseException | None = None,
        *,
        handed_off: bool = False,
    ) -> None:
        """Record one outcome and end the attempt span exactly once."""
        if self._finished:
            return
        self._finished = True
        outcome = _session_outcome(self.session, error, handed_off=handed_off)
        dimensions = _instrumentation_dimensions(
            self.session,
            operation=_observability.Operation.SESSION,
            outcome=outcome,
            error=_instrumentation_error(self, error),
        )
        _observability._increment(
            self.instrumentation,
            _observability.Counter.SESSION_OUTCOMES,
            dimensions,
        )
        if self.session.has_established and not handed_off:
            _observability._observe_duration(
                self.instrumentation,
                _observability.Duration.SESSION,
                self.observation_clock() - self.started,
                dimensions,
            )
        self.session_span.end(dimensions)
        _log_session_observation(
            self,
            _SessionObservation(
                "session",
                outcome.value,
                self.started,
                error=_instrumentation_error(self, error),
                cleanup_failures=self.cleanup_failures,
            ),
        )


def start_session_instrumentation(
    session: SessionState,
    instrumentation: SessionInstrumentation,
    clock: cabc.Callable[[], float],
) -> _SessionInstrumentationRun:
    """Start telemetry before route hooks so setup and cleanup share a span."""
    return _SessionInstrumentationRun(session, instrumentation, clock)


def start_cleanup_instrumentation(
    attempt: _SessionInstrumentationRun | None,
) -> _CleanupInstrumentation | None:
    """Start a cleanup child span only after cleanup ownership is acquired."""
    if attempt is None:
        return None
    started = attempt.observation_clock()
    dimensions = _instrumentation_dimensions(
        attempt.session,
        operation=_observability.Operation.CLEANUP,
        outcome=_observability.Outcome.STARTED,
        error=None,
    )
    span = _observability._start_span(
        attempt.instrumentation, _observability.SpanName.CLEANUP, dimensions
    )
    _log_session_observation(
        attempt, _SessionObservation("cleanup", "started", started)
    )
    return _CleanupInstrumentation(attempt, started, span)


def finish_cleanup_instrumentation(
    cleanup: _CleanupInstrumentation | None,
    failures: tuple[BaseException, ...],
) -> None:
    """Record cleanup failures and duration after the CLOSED transition."""
    if cleanup is None:
        return
    attempt = cleanup.attempt
    attempt.cleanup_failures = failures
    outcome = (
        _observability.Outcome.FAILED if failures else _observability.Outcome.COMPLETED
    )
    error = failures[0] if failures else None
    dimensions = _instrumentation_dimensions(
        attempt.session,
        operation=_observability.Operation.CLEANUP,
        outcome=outcome,
        error=error,
    )
    if failures:
        _observability._increment(
            attempt.instrumentation,
            _observability.Counter.CLEANUP_FAILURES,
            dimensions,
            len(failures),
        )
    _observability._observe_duration(
        attempt.instrumentation,
        _observability.Duration.CLEANUP,
        attempt.observation_clock() - cleanup.started,
        dimensions,
    )
    cleanup.span.end(dimensions)
    _log_session_observation(
        attempt,
        _SessionObservation(
            "cleanup",
            outcome.value,
            cleanup.started,
            error=error,
            cleanup_failures=failures,
        ),
    )


def _session_outcome(
    session: SessionState,
    error: BaseException | None,
    *,
    handed_off: bool,
) -> _observability.Outcome:
    """Classify the router attempt without unbounded labels."""
    if error is not None:
        return (
            _observability.Outcome.CANCELLED
            if isinstance(error, asyncio.CancelledError)
            else _observability.Outcome.FAILED
        )
    if session.phase is SessionPhase.CONNECTING:
        return _observability.Outcome.REJECTED
    if handed_off:
        return _observability.Outcome.HANDED_OFF
    return _observability.Outcome.COMPLETED


def _instrumentation_error(
    attempt: _SessionInstrumentationRun, error: BaseException | None
) -> BaseException | None:
    """Prefer the application cause over grouped cleanup errors."""
    if attempt.session.primary_error is not None:
        return attempt.session.primary_error
    if attempt.cleanup_failures:
        return attempt.cleanup_failures[0]
    return error


def _instrumentation_dimensions(
    session: SessionState,
    *,
    operation: _observability.Operation,
    outcome: _observability.Outcome,
    error: BaseException | None,
) -> _observability.SessionDimensions:
    """Build labels only from closed enum categories."""
    phase = {
        SessionPhase.CONNECTING: _observability.Phase.CONNECTING,
        SessionPhase.ESTABLISHED: _observability.Phase.ESTABLISHED,
        SessionPhase.DISCONNECTING: _observability.Phase.DISCONNECTING,
        SessionPhase.CLOSED: _observability.Phase.CLOSED,
    }[session.phase]
    return _observability.SessionDimensions(
        operation=operation,
        outcome=outcome,
        phase=phase,
        close_code=_observability._close_code_category(session.close_code),
        exception=_observability._exception_category(
            error,
            is_transport=isinstance(
                error, (falcon.WebSocketDisconnected, _PeerDisconnectedError)
            ),
        ),
    )
