"""Recording host adapters and resources for lifecycle observation tests."""

from __future__ import annotations

import collections as cl
import dataclasses as dc
import typing as typ

from falcon_pachinko import WebSocketResource, WebSocketRouter

if typ.TYPE_CHECKING:
    from falcon_pachinko.observability import (
        LifecycleObservationEvent,
        LifecycleSpanName,
        LifecycleSpanResult,
    )


@dc.dataclass(slots=True)
class _RecordedSpan:
    """Retain bounded completion data supplied to one host tracing span."""

    name: LifecycleSpanName
    results: list[LifecycleSpanResult] = dc.field(default_factory=list)
    finish_calls: int = 0

    def finish(self, result: LifecycleSpanResult) -> None:
        """Require one completion and retain its bounded lifecycle result."""
        self.finish_calls += 1
        if self.finish_calls != 1:
            msg = "a lifecycle span may only finish once"
            raise RuntimeError(msg)
        self.results.append(result)


@dc.dataclass(slots=True)
class _RecordingObserver:
    """Collect metric events and tracing spans at the public adapter boundary."""

    events: list[LifecycleObservationEvent] = dc.field(default_factory=list)
    spans: list[_RecordedSpan] = dc.field(default_factory=list)
    operation_counts: cl.Counter[str] = dc.field(default_factory=cl.Counter)

    def start_span(self, name: LifecycleSpanName) -> _RecordedSpan:
        """Create one recording span for a fixed framework span name."""
        span = _RecordedSpan(name)
        self.spans.append(span)
        return span

    def record_operation(self, event: LifecycleObservationEvent) -> None:
        """Retain one aggregate lifecycle operation measurement."""
        self.events.append(event)
        self.operation_counts[event.operation] += 1


@dc.dataclass(slots=True)
class _FailingSpan(_RecordedSpan):
    """Raise a host tracing failure when the framework completes the span."""

    error: BaseException = dc.field(default_factory=RuntimeError)

    def finish(self, result: LifecycleSpanResult) -> None:
        """Simulate a failing host span sink."""
        del result
        raise self.error


@dc.dataclass(slots=True)
class _FailingObserver(_RecordingObserver):
    """Fail one adapter callback without gaining control of lifecycle policy."""

    stage: str = "record"
    error: BaseException = dc.field(default_factory=RuntimeError)

    def start_span(self, name: LifecycleSpanName) -> _RecordedSpan:
        """Inject a tracing start or completion failure at the host boundary."""
        if self.stage == "start":
            raise self.error
        if self.stage == "finish":
            return _FailingSpan(name, error=self.error)
        return _RecordingObserver.start_span(self, name)

    def record_operation(self, event: LifecycleObservationEvent) -> None:
        """Inject a host metrics failure after the operation has completed."""
        if self.stage == "record":
            raise self.error
        _RecordingObserver.record_operation(self, event)


class _ObservedResource(WebSocketResource):
    """Exercise rejection, accepted setup, frame dispatch, and cleanup."""

    def __init__(
        self,
        *,
        accepted: bool = True,
        start_error: Exception | None = None,
        cleanup_error: Exception | None = None,
    ) -> None:
        """Configure the lifecycle outcome and retain its observable calls."""
        self.accepted = accepted
        self.start_error = start_error
        self.cleanup_error = cleanup_error
        self.frames: list[str | bytes] = []
        self.disconnects: list[int] = []

    async def on_connect(self, req: object, ws: object, **params: object) -> bool:
        """Return the configured pre-accept decision."""
        del req, ws, params
        return self.accepted

    async def on_session_start(self, ws: object) -> None:
        """Raise the configured accepted-session setup failure."""
        del ws
        if self.start_error is not None:
            raise self.start_error

    async def dispatch(self, ws: object, raw: str | bytes) -> None:
        """Consume frames without generating a telemetry event for each frame."""
        del ws
        self.frames.append(raw)

    async def on_disconnect(self, ws: object, close_code: int) -> None:
        """Record cleanup before returning its configured error to the driver."""
        del ws
        self.disconnects.append(close_code)
        if self.cleanup_error is not None:
            raise self.cleanup_error


def _observed_router(
    resource: _ObservedResource, observer: _RecordingObserver | None
) -> WebSocketRouter:
    """Configure a host observer without changing router construction."""
    router = WebSocketRouter()
    router.lifecycle_observer = observer
    router.add_route("/session", lambda: resource)
    router.mount("/")
    return router
