"""Optional, bounded DEBUG observations for application telemetry handlers."""

from __future__ import annotations

import asyncio
import contextlib
import time
import typing as typ

from .diagnostics import _type_name

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    import logging

_OPERATIONS = frozenset({
    "subscribe",
    "unsubscribe",
    "refresh",
    "release",
    "publish",
    "decode",
    "deliver",
    "dispatch",
    "listen",
    "receive",
    "disconnect",
    "session",
    "renew_membership",
    "run",
})


@contextlib.contextmanager
def _observe_operation(
    logger: logging.Logger, operation: str, *, emit_success: bool = True
) -> cabc.Iterator[None]:
    """Describe an operation without retaining application values.

    Handlers may convert these DEBUG records into application metrics or spans.
    ``emit_success=False`` omits normal per-frame events. Exception metadata
    contains only a bounded type name; cancellation and failures propagate.

    Yields
    ------
    None
        Control to the operation being observed.
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
    outcome = "succeeded"
    exception_type: str | None = None
    try:
        yield
    except BaseException as error:
        # Observe cancellation too, preserving the exact exception and policy.
        outcome = "cancelled" if isinstance(error, asyncio.CancelledError) else "failed"
        exception_type = _type_name(error)
        raise
    finally:
        if emit_success or outcome != "succeeded":
            logger.debug(
                "Operation observation",
                extra={
                    "operation": operation,
                    "outcome": outcome,
                    "duration_seconds": max(0.0, time.monotonic() - started),
                    "exception_type": exception_type,
                },
            )
