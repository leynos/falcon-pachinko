"""Optional, host-owned metrics and tracing for routed WebSocket sessions."""

from __future__ import annotations

import asyncio
import dataclasses as dc
import enum
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
    """Counter instruments exposed by the optional session boundary."""

    SESSION_OUTCOMES = "websocket.session.outcomes"
    CLEANUP_FAILURES = "websocket.cleanup.failures"


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
        del dimensions

    def end(  # ruff: ignore[no-self-use] - protocol shape
        self,
    ) -> None:
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


__all__ = (
    "CloseCodeCategory",
    "Counter",
    "Duration",
    "ExceptionCategory",
    "NoOpSessionInstrumentation",
    "Operation",
    "Outcome",
    "Phase",
    "SessionDimensions",
    "SessionInstrumentation",
    "SessionSpan",
    "SpanName",
)
