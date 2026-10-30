"""Emit bounded observations for routed WebSocket session behaviour."""

from __future__ import annotations

import asyncio
import dataclasses as dc
import logging
import typing as typ

from ._session import MAX_CLOSE_CODE
from .diagnostics import _type_name

if typ.TYPE_CHECKING:
    from ._session_driver import _SessionExecution

logger = logging.getLogger("falcon_pachinko._session_driver")
_OBSERVATION_OPERATIONS = frozenset({
    "session",
    "setup",
    "acceptance",
    "receive",
    "cleanup",
})
_OBSERVATION_OUTCOMES = frozenset({
    "started",
    "completed",
    "rejected",
    "failed",
    "cancelled",
})
_MAX_CLEANUP_FAILURE_TYPES = 3


@dc.dataclass(frozen=True, slots=True)
class _SessionObservation:
    """Keep bounded event metadata together at the logging boundary."""

    operation: str
    outcome: str
    started: float
    error: BaseException | None = None
    cleanup_failures: tuple[BaseException, ...] = ()


def _log_session_observation(
    execution: _SessionExecution,
    observation: _SessionObservation,
) -> None:
    """Emit bounded lifecycle metadata without reflecting caller values."""
    operation = observation.operation
    operation = operation if operation in _OBSERVATION_OPERATIONS else "other"
    outcome = observation.outcome
    outcome = outcome if outcome in _OBSERVATION_OUTCOMES else "other"
    failure_types = tuple(
        _type_name(failure)
        for failure in observation.cleanup_failures[:_MAX_CLEANUP_FAILURE_TYPES]
    )
    close_code = execution.session.close_code
    if type(close_code) is not int or not 0 <= close_code <= MAX_CLOSE_CODE:
        close_code = None
    logger.debug(
        "WebSocket session observation",
        extra={
            "operation": operation,
            "outcome": outcome,
            "phase": execution.session.phase.value,
            "close_code": close_code,
            "duration_seconds": max(
                0.0, execution.observation_clock() - observation.started
            ),
            "resource_type": _type_name(execution.resource),
            "exception_type": (
                _type_name(observation.error) if observation.error is not None else None
            ),
            "primary_exception_type": (
                _type_name(execution.primary_error)
                if execution.primary_error is not None
                else None
            ),
            "cleanup_failure_count": len(observation.cleanup_failures),
            "cleanup_failures_truncated": (
                len(observation.cleanup_failures) > _MAX_CLEANUP_FAILURE_TYPES
            ),
            "cleanup_failure_types": failure_types,
        },
    )


def _outcome_for_error(error: BaseException | None) -> str:
    """Map a session error to a bounded lifecycle outcome."""
    if error is None:
        return "completed"
    if isinstance(error, asyncio.CancelledError):
        return "cancelled"
    return "failed"


def _cleanup_outcome(
    failures: tuple[BaseException, ...],
    interruptions: tuple[asyncio.CancelledError, ...],
) -> str:
    """Map cleanup failures and interruptions to a bounded outcome."""
    if failures:
        return "failed"
    if interruptions:
        return "cancelled"
    return "completed"
