"""Optional, host-owned metrics and tracing for routed WebSocket sessions."""

from __future__ import annotations

import asyncio
import collections
import dataclasses as dc
import enum
import logging
import typing as typ

if typ.TYPE_CHECKING:
    import collections.abc as cabc


class Operation(enum.StrEnum):
    """Instrumented lifecycle operation."""

    SESSION = "session"
    CLEANUP = "cleanup"


class Outcome(enum.StrEnum):
    """Fixed outcomes emitted by session and cleanup instrumentation."""

    STARTED = "started"
    COMPLETED = "completed"
    REJECTED = "rejected"
    HANDED_OFF = "handed_off"
    FAILED = "failed"
    CANCELLED = "cancelled"


class Phase(enum.StrEnum):
    """Fixed lifecycle phases available as an instrumentation dimension."""

    CONNECTING = "connecting"
    ESTABLISHED = "established"
    DISCONNECTING = "disconnecting"
    CLOSED = "closed"


class CloseCodeCategory(enum.StrEnum):
    """Bounded categories for effective WebSocket close codes."""

    UNKNOWN = "unknown"
    NORMAL = "normal"
    GOING_AWAY = "going_away"
    ABNORMAL = "abnormal"
    PROTOCOL = "protocol"
    UNSUPPORTED_DATA = "unsupported_data"
    POLICY = "policy"
    INTERNAL_ERROR = "internal_error"
    APPLICATION = "application"
    OTHER = "other"


class ExceptionCategory(enum.StrEnum):
    """Bounded categories that never expose exception type or value."""

    NONE = "none"
    CANCELLATION = "cancellation"
    TRANSPORT = "transport"
    ORDINARY = "ordinary"
    GROUP = "group"
    BASE = "base"


class Counter(enum.StrEnum):
    """Counter instruments exposed by the optional instrumentation boundaries."""

    SESSION_OUTCOMES = "websocket.session.outcomes"
    CLEANUP_FAILURES = "websocket.cleanup.failures"
    SCHEMA_DISPATCH_OUTCOMES = "websocket.schema_dispatch.outcomes"


class Duration(enum.StrEnum):
    """Duration measurements exposed by the optional session boundary."""

    SESSION = "websocket.session.duration"
    CLEANUP = "websocket.cleanup.duration"


class SpanName(enum.StrEnum):
    """Span names exposed by the optional session boundary."""

    SESSION = "websocket.session"
    CLEANUP = "websocket.cleanup"


@dc.dataclass(frozen=True, slots=True)
class SessionDimensions:
    """Closed set of lifecycle dimensions safe for metric labels."""

    operation: Operation
    outcome: Outcome
    phase: Phase
    close_code: CloseCodeCategory
    exception: ExceptionCategory


class SessionSpan(typ.Protocol):
    """Synchronous span handle activated in the current context on start."""

    def set_dimensions(self, dimensions: SessionDimensions) -> None:
        """Set final bounded dimensions before ending this span."""

    def end(self) -> None:
        """End this span and restore its previous current-span context."""


class SessionInstrumentation(typ.Protocol):
    """Non-blocking synchronous adapter supplied by the hosting application.

    Implementations should enqueue exporter work to a bounded queue and drop or
    aggregate when that queue is full. ``start_span`` must activate its returned
    span using context-local state and must either succeed atomically or leave
    that state unchanged. The router copies the active context into its owned
    cleanup task so the cleanup span can be a child of the session span.
    """

    def increment(
        self,
        counter: Counter,
        dimensions: SessionDimensions,
        amount: int = 1,
    ) -> None:
        """Add ``amount`` to a fixed counter with bounded dimensions."""

    def observe_duration(
        self,
        duration: Duration,
        seconds: float,
        dimensions: SessionDimensions,
    ) -> None:
        """Record a monotonic duration with bounded dimensions."""

    def start_span(
        self,
        name: SpanName,
        dimensions: SessionDimensions,
    ) -> SessionSpan:
        """Start and activate one span in the current execution context."""


class _NoOpSpan:
    """Span handle for the default no-op implementation."""

    def set_dimensions(  # ruff: ignore[no-self-use] - protocol shape
        self, dimensions: SessionDimensions
    ) -> None:
        """Discard the dimensions because this span keeps no state."""
        del dimensions

    def end(  # ruff: ignore[no-self-use] - protocol shape
        self,
    ) -> None:
        """Do nothing, because the no-op span has nothing to finish."""
        return


class NoOpSessionInstrumentation:
    """Keep session instrumentation disabled without host configuration."""

    def increment(  # ruff: ignore[no-self-use] - protocol shape
        self,
        counter: Counter,
        dimensions: SessionDimensions,
        amount: int = 1,
    ) -> None:
        """Accept an outcome without recording it."""
        del counter, dimensions, amount

    def observe_duration(  # ruff: ignore[no-self-use] - protocol shape
        self,
        duration: Duration,
        seconds: float,
        dimensions: SessionDimensions,
    ) -> None:
        """Accept a duration without recording it."""
        del duration, seconds, dimensions

    def start_span(  # ruff: ignore[no-self-use] - protocol shape
        self,
        name: SpanName,
        dimensions: SessionDimensions,
    ) -> SessionSpan:
        """Return a no-op span handle."""
        del name, dimensions
        return _NoOpSpan()


NOOP_SESSION_INSTRUMENTATION = NoOpSessionInstrumentation()

# Bounded outcome labels for schema-dispatch observations. The dispatcher
# coerces an unexpected value to ``other``, and the counter below rejects a
# label it does not know, so neither can grow an unbounded label set.
SCHEMA_DISPATCH_OUTCOMES = frozenset({
    "delivered",
    "malformed",
    "unknown_tag",
    "no_handler",
    "rejected_fields",
    "conversion_failed",
    "configuration_error",
})

_SCHEMA_DISPATCH_MESSAGE = "Schema dispatch observation"
_DISPATCHER_LOGGER = "falcon_pachinko.dispatcher"


class SchemaDispatchCounter(logging.Handler):
    """Count schema-dispatch outcomes from the dispatcher's DEBUG records.

    Attach the handler to the ``falcon_pachinko.dispatcher`` logger. Records
    from another logger, records with another message, and records whose
    ``outcome`` is outside :data:`SCHEMA_DISPATCH_OUTCOMES` are ignored, so the
    counter cannot grow an unbounded label set.

    Counts are unsampled: they cover exactly the records this handler receives.
    If DEBUG logging is disabled, filtered or sampled for that logger, the
    counter sees only what survives, so treat the totals as a lower bound.
    """

    counter_name = Counter.SCHEMA_DISPATCH_OUTCOMES

    def __init__(self) -> None:
        """Start with no counted outcomes."""
        super().__init__(level=logging.DEBUG)
        self._counts: collections.Counter[str] = collections.Counter()

    def emit(self, record: logging.LogRecord) -> None:
        """Count one bounded schema-dispatch outcome, ignoring anything else."""
        if record.name != _DISPATCHER_LOGGER:
            return
        if record.getMessage() != _SCHEMA_DISPATCH_MESSAGE:
            return
        outcome = getattr(record, "outcome", None)
        if outcome not in SCHEMA_DISPATCH_OUTCOMES:
            return
        self._counts[outcome] += 1

    def counts(self) -> dict[str, int]:
        """Return a snapshot of the counts keyed by outcome label."""
        return dict(self._counts)

    def rejection_ratio(self) -> float:
        """Return rejected frames over counted frames, or ``0.0`` when empty.

        ``delivered`` is the only accepted outcome, so every other label counts
        as a rejection. A zero denominator returns ``0.0`` rather than raising,
        so a fresh counter is safe to export before traffic arrives.

        Returns
        -------
        float
            Rejected frames divided by counted frames, in ``[0.0, 1.0]``.
        """
        total = sum(self._counts.values())
        if total == 0:
            return 0.0
        return (total - self._counts.get("delivered", 0)) / total


_APPLICATION_CLOSE_CODE_MIN = 3000
_APPLICATION_CLOSE_CODE_MAX = 4999


def _close_code_category(close_code: int | None) -> CloseCodeCategory:
    """Bound numeric close codes to a small, fixed category vocabulary."""
    known = {
        1000: CloseCodeCategory.NORMAL,
        1001: CloseCodeCategory.GOING_AWAY,
        1002: CloseCodeCategory.PROTOCOL,
        1003: CloseCodeCategory.UNSUPPORTED_DATA,
        1006: CloseCodeCategory.ABNORMAL,
        1008: CloseCodeCategory.POLICY,
        1011: CloseCodeCategory.INTERNAL_ERROR,
    }
    if close_code is None:
        return CloseCodeCategory.UNKNOWN
    if close_code in known:
        return known[close_code]
    if _APPLICATION_CLOSE_CODE_MIN <= close_code <= _APPLICATION_CLOSE_CODE_MAX:
        return CloseCodeCategory.APPLICATION
    return CloseCodeCategory.OTHER


def _exception_category(
    error: BaseException | None, *, is_transport: bool = False
) -> ExceptionCategory:
    """Map exceptions to fixed classes without exposing names or values."""
    match error:
        case None:
            return ExceptionCategory.NONE
        case asyncio.CancelledError():
            return ExceptionCategory.CANCELLATION
        case _ if is_transport:
            return ExceptionCategory.TRANSPORT
        case BaseExceptionGroup():
            return ExceptionCategory.GROUP
        case Exception():
            return ExceptionCategory.ORDINARY
        case _:
            return ExceptionCategory.BASE


class _SpanOwner:
    """Isolate span callbacks and make ending idempotent at the boundary."""

    def __init__(
        self,
        instrumentation: SessionInstrumentation,
        name: SpanName,
        dimensions: SessionDimensions,
    ) -> None:
        """Keep the instrumentation, span name and dimensions until start()."""
        self._instrumentation = instrumentation
        self._name = name
        self._dimensions = dimensions
        self._span: SessionSpan | None = None
        self._started = False
        self._ended = False

    def start(self) -> None:
        """Start the span once; instrumentation errors leave a no-op handle."""
        if self._started:
            return
        self._started = True
        self._span = _safe_call(
            lambda: self._instrumentation.start_span(self._name, self._dimensions)
        )

    def end(self, dimensions: SessionDimensions) -> None:
        """Set final dimensions and attempt to end the span exactly once."""
        if self._ended:
            return
        self._ended = True
        span = self._span
        if span is None:
            return
        try:
            _safe_call(lambda: span.set_dimensions(dimensions))
        finally:
            _safe_call(lambda: span.end())  # ruff: ignore[unnecessary-lambda] - guard lookup


def _safe_call[R](callback: cabc.Callable[[], R]) -> R | None:
    """Keep ordinary telemetry errors and callback cancellation out of routing."""
    try:
        return callback()
    except (Exception, asyncio.CancelledError):  # ruff: ignore[blind-except] - isolate telemetry
        return None


def _start_span(
    instrumentation: SessionInstrumentation,
    name: SpanName,
    dimensions: SessionDimensions,
) -> _SpanOwner:
    """Create a guarded span owner before entering asynchronous work."""
    owner = _SpanOwner(instrumentation, name, dimensions)
    owner.start()
    return owner


def _increment(
    instrumentation: SessionInstrumentation,
    counter: Counter,
    dimensions: SessionDimensions,
    amount: int = 1,
) -> None:
    """Emit one counter update without allowing it to affect session policy."""
    _safe_call(lambda: instrumentation.increment(counter, dimensions, amount))


def _observe_duration(
    instrumentation: SessionInstrumentation,
    duration: Duration,
    seconds: float,
    dimensions: SessionDimensions,
) -> None:
    """Emit a non-negative duration without allowing it to affect the session."""
    _safe_call(
        lambda: instrumentation.observe_duration(
            duration, max(0.0, seconds), dimensions
        )
    )


type LifecycleOperation = typ.Literal[
    "negotiation",
    "acceptance",
    "session_start",
    "receive_termination",
    "finalization",
    "cleanup",
]
type LifecycleOutcome = typ.Literal["succeeded", "rejected", "failed", "cancelled"]
type LifecycleFailureCategory = typ.Literal[
    "none",
    "application",
    "transport",
    "cleanup",
    "cancelled",
    "other",
]
type LifecycleCloseCodeCategory = typ.Literal[
    "unknown",
    "normal",
    "going_away",
    "protocol_error",
    "server_error",
    "abnormal",
    "other",
]
type LifecycleSpanName = typ.Literal[
    "falcon_pachinko.websocket.negotiation",
    "falcon_pachinko.websocket.session",
    "falcon_pachinko.websocket.finalization",
]


@dc.dataclass(frozen=True, slots=True)
class LifecycleSpanResult:
    """Bounded completion attributes for one host tracing span.

    Attributes
    ----------
    outcome : LifecycleOutcome
        Whether the observed lifecycle scope succeeded, rejected, failed, or
        was cancelled.
    failure_category : LifecycleFailureCategory
        Fixed failure classification, without an exception type or value.
    close_code_category : LifecycleCloseCodeCategory
        Fixed classification of the observed close code, or ``unknown``.
    """

    outcome: LifecycleOutcome
    failure_category: LifecycleFailureCategory
    close_code_category: LifecycleCloseCodeCategory


@dc.dataclass(frozen=True, slots=True)
class LifecycleObservationEvent:
    """One completed lifecycle operation for host metrics.

    Attributes
    ----------
    operation : LifecycleOperation
        One of the framework's six aggregate lifecycle operations.
    outcome : LifecycleOutcome
        Completion outcome, including ordinary pre-accept rejection.
    duration_seconds : float
        Nonnegative duration measured with a monotonic clock.
    failure_category : LifecycleFailureCategory
        Fixed failure classification; exception values are never retained.
    close_code_category : LifecycleCloseCodeCategory
        Fixed close-code classification; raw codes are never metric labels.
    """

    operation: LifecycleOperation
    outcome: LifecycleOutcome
    duration_seconds: float
    failure_category: LifecycleFailureCategory
    close_code_category: LifecycleCloseCodeCategory


class LifecycleSpan(typ.Protocol):
    """A host-owned span that can be completed without a telemetry SDK dependency."""

    def finish(self, result: LifecycleSpanResult) -> None:
        """Finish the span exactly once with bounded lifecycle attributes.

        Parameters
        ----------
        result : LifecycleSpanResult
            Final outcome, failure category, and close-code category.
        """


class LifecycleObserver(typ.Protocol):
    """Synchronous host metrics and tracing adapter configured on a router.

    Implementations must return promptly. Framework calls are best effort:
    adapter exceptions and adapter-raised cancellation do not change the
    observed WebSocket operation. Configure the adapter before serving.
    """

    def start_span(self, name: LifecycleSpanName) -> LifecycleSpan | None:
        """Start an optional host span for one of the three fixed names.

        Parameters
        ----------
        name : LifecycleSpanName
            The framework span name, containing no application data.

        Returns
        -------
        LifecycleSpan or None
            A span to finish, or ``None`` when host tracing is disabled.
        """

    def record_operation(self, event: LifecycleObservationEvent) -> None:
        """Record one counter increment and duration histogram observation.

        Parameters
        ----------
        event : LifecycleObservationEvent
            Increment ``falcon_pachinko.websocket.lifecycle.operations`` and
            observe ``falcon_pachinko.websocket.lifecycle.operation.duration``
            in seconds, using its four bounded categorical fields as labels.
        """


__all__ = (
    "SCHEMA_DISPATCH_OUTCOMES",
    "CloseCodeCategory",
    "Counter",
    "Duration",
    "ExceptionCategory",
    "LifecycleCloseCodeCategory",
    "LifecycleFailureCategory",
    "LifecycleObservationEvent",
    "LifecycleObserver",
    "LifecycleOperation",
    "LifecycleOutcome",
    "LifecycleSpan",
    "LifecycleSpanName",
    "LifecycleSpanResult",
    "NoOpSessionInstrumentation",
    "Operation",
    "Outcome",
    "Phase",
    "SessionDimensions",
    "SessionInstrumentation",
    "SessionSpan",
    "SpanName",
)
