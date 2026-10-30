"""Optional, bounded DEBUG observations for application telemetry handlers."""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses as dc
import time
import typing as typ

from .diagnostics import _type_name
from .observability import LifecycleObservationEvent, LifecycleSpanResult

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    import logging

    from .observability import (
        LifecycleCloseCodeCategory,
        LifecycleFailureCategory,
        LifecycleObserver,
        LifecycleOperation,
        LifecycleOutcome,
        LifecycleSpan,
        LifecycleSpanName,
    )

_OPERATIONS = frozenset({
    "subscribe",
    "unsubscribe",
    "refresh",
    "release",
    "publish",
    "decode",
    "deliver",
    "listen",
    "receive",
    "disconnect",
    "session",
    "renew_membership",
    "run",
})


@dc.dataclass(slots=True)
class _LogOperationObservation:
    """Allow best-effort operations to report failures returned as values."""

    outcome: str = "succeeded"
    exception_type: str | None = None

    def record_failure(self, error: BaseException) -> None:
        """Retain safe metadata without retaining the exception itself."""
        if self.outcome == "cancelled":
            return
        self.outcome = (
            "cancelled" if isinstance(error, asyncio.CancelledError) else "failed"
        )
        self.exception_type = _type_name(error)


@contextlib.contextmanager
def _observe_operation(
    logger: logging.Logger, operation: str, *, emit_success: bool = True
) -> cabc.Iterator[_LogOperationObservation]:
    """Describe an operation without retaining application values.

    Handlers may convert these DEBUG records into application metrics or spans.
    ``emit_success=False`` omits normal per-frame events. Exception metadata
    contains only a bounded type name; cancellation and failures propagate.

    Yields
    ------
    _LogOperationObservation
        Completion state for failures returned rather than raised.
    """
    operation = operation if operation in _OPERATIONS else "other"
    started = time.monotonic()
    if emit_success:
        logger.debug(
            "Operation observation",
            extra={
                "operation": operation,
                "outcome": "started",
                "duration_seconds": 0.0,
                "exception_type": None,
            },
        )
    observation = _LogOperationObservation()
    try:
        yield observation
    except BaseException as error:
        # Observe cancellation too, preserving the exact exception and policy.
        observation.record_failure(error)
        raise
    finally:
        if emit_success or observation.outcome != "succeeded":
            logger.debug(
                "Operation observation",
                extra={
                    "operation": operation,
                    "outcome": observation.outcome,
                    "duration_seconds": max(0.0, time.monotonic() - started),
                    "exception_type": observation.exception_type,
                },
            )


@dc.dataclass(slots=True)
class _LifecycleObservation:
    """Accumulate only fixed lifecycle categories until an operation completes."""

    outcome: LifecycleOutcome = "succeeded"
    failure_category: LifecycleFailureCategory = "none"
    close_code_category: LifecycleCloseCodeCategory = "unknown"
    started: float = dc.field(default=0.0, repr=False)

    def reject(self) -> None:
        """Distinguish an ordinary pre-accept decision from a failure."""
        self.outcome = "rejected"

    def record_failure(
        self, error: BaseException, category: LifecycleFailureCategory
    ) -> None:
        """Classify failures without retaining exception types or values."""
        if isinstance(error, asyncio.CancelledError):
            self.outcome = "cancelled"
            self.failure_category = "cancelled"
        elif self.outcome != "cancelled":
            self.outcome = "failed"
            self.failure_category = category

    def set_close_code(self, code: int | None) -> None:
        """Map raw close codes into the fixed metric category vocabulary."""
        self.close_code_category = _close_code_category(code)


@contextlib.contextmanager
def _observe_lifecycle(
    observer: LifecycleObserver | None,
    operation: LifecycleOperation | None = None,
    *,
    span_name: LifecycleSpanName | None = None,
    failure_category: LifecycleFailureCategory = "other",
) -> cabc.Iterator[_LifecycleObservation]:
    """Complete bounded metrics and spans without changing lifecycle policy.

    An absent observer makes no host callbacks or clock reads. Synchronous
    adapter failures are isolated from the operation, including an adapter
    that raises ``CancelledError`` rather than an ordinary exception.

    Yields
    ------
    _LifecycleObservation
        State used to report rejection, returned failures, and close codes.
    """
    observation = _LifecycleObservation()
    if observer is None:
        yield observation
        return

    observation.started = time.monotonic()
    span: LifecycleSpan | None = None
    if span_name is not None:
        with contextlib.suppress(Exception, asyncio.CancelledError):
            span = observer.start_span(span_name)
    try:
        yield observation
    except BaseException as error:
        if (
            isinstance(error, asyncio.CancelledError)
            or observation.outcome == "succeeded"
        ):
            observation.record_failure(error, failure_category)
        raise
    finally:
        _complete_lifecycle_observation(observer, operation, span, observation)


def _complete_lifecycle_observation(
    observer: LifecycleObserver,
    operation: LifecycleOperation | None,
    span: LifecycleSpan | None,
    observation: _LifecycleObservation,
) -> None:
    """Publish immutable completion data through isolated host callbacks."""
    if operation is not None:
        event = LifecycleObservationEvent(
            operation=operation,
            outcome=observation.outcome,
            duration_seconds=max(0.0, time.monotonic() - observation.started),
            failure_category=observation.failure_category,
            close_code_category=observation.close_code_category,
        )
        with contextlib.suppress(Exception, asyncio.CancelledError):
            observer.record_operation(event)
    if span is not None:
        result = LifecycleSpanResult(
            outcome=observation.outcome,
            failure_category=observation.failure_category,
            close_code_category=observation.close_code_category,
        )
        with contextlib.suppress(Exception, asyncio.CancelledError):
            span.finish(result)


def _close_code_category(code: int | None) -> LifecycleCloseCodeCategory:
    """Group standard and application close codes without unbounded labels."""
    category: LifecycleCloseCodeCategory = "other"
    match code:
        case None:
            category = "unknown"
        case 1000:
            category = "normal"
        case 1001:
            category = "going_away"
        case 1006 | 1015:
            category = "abnormal"
        case 1011 | 1012 | 1013 | 1014:
            category = "server_error"
        case 1002 | 1003 | 1007 | 1008 | 1009 | 1010:
            category = "protocol_error"
    return category
