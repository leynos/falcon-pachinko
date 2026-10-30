"""Recording collaborators for routed-session instrumentation tests."""

from __future__ import annotations

import asyncio
import contextvars
import typing as typ

from falcon_pachinko.observability import (
    CloseCodeCategory,
    Counter,
    Duration,
    ExceptionCategory,
    Operation,
    Outcome,
    Phase,
    SessionDimensions,
    SessionSpan,
    SpanName,
)
from falcon_pachinko.router import WebSocketRouter
from falcon_pachinko.unittests._session_fixtures import (
    RecordingResource,
    SessionSocket,
    make_session_request,
)

_ASSERTION_FAILURE = "router instrumentation must preserve bounded lifecycle semantics"


class InjectedClock:
    """Return a controlled monotonic value changed by resource callbacks."""

    def __init__(self) -> None:
        self.value = 0.0
        self.readings: list[float] = []

    def __call__(self) -> float:
        """Record and return the current injected monotonic value."""
        self.readings.append(self.value)
        return self.value


class RecordedSpan:
    """Record span nesting, dimensions, and exactly-once completion."""

    def __init__(
        self,
        owner: RecordingInstrumentation,
        name: SpanName,
        dimensions: SessionDimensions,
    ) -> None:
        self.owner = owner
        self.name = name
        self.parent = owner.active_span.get()
        self.dimensions = dimensions
        self.end_count = 0
        self._token = owner.active_span.set(self)

    def set_dimensions(self, dimensions: SessionDimensions) -> None:
        """Store the final bounded dimensions for this span."""
        self.dimensions = dimensions

    def end(self) -> None:
        """Restore the active span that preceded this one."""
        self.end_count += 1
        self.owner.active_span.reset(self._token)


class RecordingInstrumentation:
    """Collect synchronous telemetry events and their bounded dimensions."""

    def __init__(self) -> None:
        self.active_span: contextvars.ContextVar[RecordedSpan | None] = (
            contextvars.ContextVar("active_test_span", default=None)
        )
        self.counters: list[tuple[Counter, int, SessionDimensions]] = []
        self.durations: list[tuple[Duration, float, SessionDimensions]] = []
        self.spans: list[RecordedSpan] = []

    def increment(
        self,
        counter: Counter,
        dimensions: SessionDimensions,
        amount: int = 1,
    ) -> None:
        """Record one counter event."""
        self.counters.append((counter, amount, dimensions))

    def observe_duration(
        self,
        duration: Duration,
        seconds: float,
        dimensions: SessionDimensions,
    ) -> None:
        """Record one duration measurement."""
        self.durations.append((duration, seconds, dimensions))

    def start_span(
        self,
        name: SpanName,
        dimensions: SessionDimensions,
    ) -> SessionSpan:
        """Activate and return a recording span."""
        span = RecordedSpan(self, name, dimensions)
        self.spans.append(span)
        return span


class InstrumentationCancelledError(asyncio.CancelledError):
    """Distinguish a telemetry cancellation from caller cancellation."""

    def __init__(self) -> None:
        """Supply a distinct message without weakening the test call site."""
        super().__init__("instrumentation cancellation")


class FailingInstrumentation:
    """Raise telemetry failures, including cancellation, at every boundary."""

    def increment(
        self,
        counter: Counter,
        dimensions: SessionDimensions,
        amount: int = 1,
    ) -> None:
        """Raise cancellation from a counter callback."""
        del counter, dimensions, amount
        raise InstrumentationCancelledError

    def observe_duration(
        self,
        duration: Duration,
        seconds: float,
        dimensions: SessionDimensions,
    ) -> None:
        """Raise an ordinary failure from a duration callback."""
        del duration, seconds, dimensions
        raise RuntimeError

    def start_span(
        self,
        name: SpanName,
        dimensions: SessionDimensions,
    ) -> SessionSpan:
        """Raise an ordinary failure while starting a span."""
        del name, dimensions
        raise RuntimeError


class FailingSpan:
    """Fail span updates and completion after recording each completion try."""

    def __init__(self) -> None:
        self.end_count = 0

    def set_dimensions(self, dimensions: SessionDimensions) -> None:
        """Raise while recording final span dimensions."""
        del dimensions
        raise RuntimeError

    def end(self) -> None:
        """Raise cancellation after recording the end attempt."""
        self.end_count += 1
        raise asyncio.CancelledError


class FailingSpanInstrumentation(FailingInstrumentation):
    """Return spans whose finalization callbacks fail independently."""

    def __init__(self) -> None:
        self.spans: list[FailingSpan] = []

    def start_span(
        self,
        name: SpanName,
        dimensions: SessionDimensions,
    ) -> SessionSpan:
        """Create a span that fails during finalization."""
        del name, dimensions
        span = FailingSpan()
        self.spans.append(span)
        return span


def configured_router(
    resource: RecordingResource,
    instrumentation: object,
    clock: InjectedClock,
    *,
    receive_loop: bool = True,
) -> WebSocketRouter:
    """Configure observability through the public router boundary."""
    router = WebSocketRouter(receive_loop=receive_loop)
    router.add_route("/session", lambda: resource)
    router.mount("/")
    router.configure_session_instrumentation(typ.cast("typ.Any", instrumentation))
    router._session_instrumentation_clock = clock.__call__
    return router


async def drive_router(router: WebSocketRouter, socket: SessionSocket) -> None:
    """Run the production responder with the same shape as Falcon dispatch."""
    await router.on_websocket(
        typ.cast("typ.Any", make_session_request()), typ.cast("typ.Any", socket)
    )


def session_counter(
    instrumentation: RecordingInstrumentation,
) -> tuple[Counter, int, SessionDimensions]:
    """Return the sole session outcome event for one routed attempt."""
    events = [
        event
        for event in instrumentation.counters
        if event[0] is Counter.SESSION_OUTCOMES
    ]
    assert len(events) == 1, _ASSERTION_FAILURE
    return events[0]


def assert_bounded_dimensions(dimensions: SessionDimensions) -> None:
    """Require every exported dimension to use its closed enum vocabulary."""
    assert isinstance(dimensions.operation, Operation), _ASSERTION_FAILURE
    assert isinstance(dimensions.outcome, Outcome), _ASSERTION_FAILURE
    assert isinstance(dimensions.phase, Phase), _ASSERTION_FAILURE
    assert isinstance(dimensions.close_code, CloseCodeCategory), _ASSERTION_FAILURE
    assert isinstance(dimensions.exception, ExceptionCategory), _ASSERTION_FAILURE
