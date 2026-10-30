"""Optional host adapters for bounded WebSocket lifecycle metrics and tracing."""

from __future__ import annotations

import dataclasses as dc
import typing as typ

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
