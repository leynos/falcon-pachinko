"""Run shielded disconnect cleanup and apply session failure precedence.

The session driver uses this module after accepted setup or receive work, and
``WebSocketSession.aclose()`` reuses it for manually managed sessions. The
cleanup task claims the single disconnect guard, attempts both hook stages,
and keeps the primary session failure ahead of cleanup failures while
preserving bare cancellation.
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import logging
import typing as typ

from ._observability import _LifecycleOptions, _observe_lifecycle, _observe_operation
from ._session import (
    MAX_CLOSE_CODE,
    SessionState,
    _report_session_failure,
    _wait_for_shielded_task,
    effective_close_code,
)
from ._session_instrumentation import (
    finish_cleanup_instrumentation,
    start_cleanup_instrumentation,
)
from .hooks import HookContext, HookEvent

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from ._session_context import SessionDriverContext

logger = logging.getLogger(__name__)


async def run_shielded_cleanup(
    context: SessionDriverContext,
) -> tuple[tuple[BaseException, ...], tuple[asyncio.CancelledError, ...]]:
    """Finish one cleanup task even if its caller receives more cancellation."""
    cleanup_task = asyncio.create_task(
        _collect_cleanup_failures(context), context=contextvars.copy_context()
    )
    interruptions = await _wait_for_shielded_task(cleanup_task)

    if cleanup_task.cancelled():
        failures = (asyncio.CancelledError(),)
    else:
        try:
            failures = cleanup_task.result()
        except Exception as exc:  # ruff: ignore[blind-except] - retain task failures
            failures = (exc,)
        except asyncio.CancelledError as exc:
            failures = (exc,)
    return failures, tuple(interruptions)


async def _collect_cleanup_failures(
    context: SessionDriverContext,
) -> tuple[BaseException, ...]:
    """Keep unexpected housekeeping errors within the session failure policy."""
    # A cancelled shield may independently report an inner task's exception.
    # Return errors as values so our precedence policy owns their single report.
    try:
        return await _cleanup_session(context)
    except BaseException as exc:  # ruff: ignore[blind-except] - retain cleanup task failures
        return (exc,)


async def _cleanup_session(context: SessionDriverContext) -> tuple[BaseException, ...]:
    """Close best-effort, notify both cleanup layers, then mark the session."""
    session = context.session_state
    if not session.begin_disconnect():
        return ()

    failures: list[BaseException] = []
    instrumentation = start_cleanup_instrumentation(context.instrumentation_run)
    with (
        _observe_operation(logger, "disconnect") as log_observation,
        _observe_lifecycle(
            context.observer,
            "cleanup",
            options=_LifecycleOptions(
                failure_category="cleanup",
                monotonic_clock=context.monotonic_clock,
            ),
        ) as observation,
    ):
        try:
            close_code = await _close_failed_session(context, failures)
            failures.extend(await _run_disconnect_hooks(context, close_code))
            for failure in failures:
                observation.record_failure(failure, "cleanup")
                log_observation.record_failure(failure)
        finally:
            observation.set_close_code(session.close_code)
            session.mark_closed()
            finish_cleanup_instrumentation(instrumentation, tuple(failures))

    return tuple(failures)


async def _close_failed_session(
    context: SessionDriverContext, failures: list[BaseException]
) -> int:
    """Choose the effective close code and close a failed session best-effort."""
    primary_error = context.primary_error
    recorded_code = getattr(context.dispatch.ws, "close_code", None)
    close_code = effective_close_code(
        recorded_code, context.session_state, primary_error
    )
    context.session_state.close_code = close_code
    should_close = primary_error is not None and not isinstance(
        primary_error, asyncio.CancelledError
    )
    if should_close and not bool(getattr(context.dispatch.ws, "closed", False)):
        try:
            await context.dispatch.ws.close(close_code)
        except BaseException as exc:  # ruff: ignore[blind-except] - cleanup must continue
            failures.append(exc)
    return close_code


async def _run_disconnect_hooks(
    context: SessionDriverContext, close_code: int
) -> tuple[BaseException, ...]:
    """Attempt both cleanup hooks and preserve every hook failure."""
    dispatch = context.dispatch
    hook_context = HookContext(
        event=HookEvent.BEFORE_DISCONNECT,
        target=context.resource,
        resource=None,
        req=dispatch.req,
        ws=dispatch.ws,
        params=dict(context.params),
        close_code=close_code,
        error=context.primary_error,
    )
    failures: list[BaseException] = []
    await _attempt_cleanup(
        lambda: context.hook_manager.notify_before_disconnect(hook_context), failures
    )
    await _attempt_cleanup(
        lambda: context.resource.on_disconnect(dispatch.ws, close_code), failures
    )
    return tuple(failures)


async def _attempt_cleanup(
    cleanup: cabc.Callable[[], cabc.Awaitable[None]],
    failures: list[BaseException],
) -> None:
    """Record a cleanup failure so later cleanup stages still run."""
    try:
        await cleanup()
    except BaseException as exc:  # ruff: ignore[blind-except] - cleanup must continue
        failures.append(exc)


def raise_session_failures(
    primary_error: BaseException | None,
    cleanup_failures: tuple[BaseException, ...],
    interruptions: tuple[asyncio.CancelledError, ...],
    *,
    session: SessionState | None = None,
) -> None:
    """Apply primary-first exception precedence after cleanup has completed."""
    cancellation = _session_cancellation(primary_error, cleanup_failures, interruptions)
    if cancellation is not None:
        _report_cancelled_session_failures(
            primary_error, cleanup_failures, cancellation, session
        )
        raise cancellation

    _raise_uncancelled_session_failures(primary_error, cleanup_failures)


def _raise_uncancelled_session_failures(
    primary_error: BaseException | None,
    cleanup_failures: tuple[BaseException, ...],
) -> None:
    """Apply primary-first grouping after cancellation precedence is settled."""
    failures = ([primary_error] if primary_error is not None else []) + list(
        cleanup_failures
    )
    if not failures:
        return
    if len(failures) == 1:
        raise failures[0]
    if all(isinstance(failure, Exception) for failure in failures):
        exceptions = [typ.cast("Exception", failure) for failure in failures]
        raise ExceptionGroup("session", exceptions)
    _raise_base_exception_failures(primary_error, cleanup_failures, failures)


def _raise_base_exception_failures(
    primary_error: BaseException | None,
    cleanup_failures: tuple[BaseException, ...],
    failures: list[BaseException],
) -> typ.NoReturn:
    """Keep base exceptions primary while reporting secondary cleanup errors."""
    if primary_error is not None and not isinstance(primary_error, Exception):
        for failure in cleanup_failures:
            _report_session_failure(
                failure,
                "WebSocket disconnect cleanup failed after a base exception",
                suppress_handler_errors=True,
            )
        raise primary_error
    raise BaseExceptionGroup("session", failures)


def _session_cancellation(
    primary_error: BaseException | None,
    cleanup_failures: tuple[BaseException, ...],
    interruptions: tuple[asyncio.CancelledError, ...],
) -> asyncio.CancelledError | None:
    """Return cancellation with precedence over ordinary session failures."""
    if isinstance(primary_error, asyncio.CancelledError):
        return primary_error
    for failure in cleanup_failures:
        if isinstance(failure, asyncio.CancelledError):
            return failure
    return interruptions[0] if interruptions else None


def _report_cancelled_session_failures(
    primary_error: BaseException | None,
    cleanup_failures: tuple[BaseException, ...],
    cancellation: asyncio.CancelledError,
    session: SessionState | None,
) -> None:
    """Report failures hidden by cancellation through the running loop."""
    if primary_error is not None and primary_error is not cancellation:
        _report_cancelled_failure(
            primary_error,
            "A WebSocket session failure was superseded by cancellation",
            session,
        )
    for failure in cleanup_failures:
        if not isinstance(failure, asyncio.CancelledError):
            _report_cancelled_failure(
                failure,
                "WebSocket disconnect cleanup failed during cancellation",
                session,
            )


def _report_cancelled_failure(
    error: BaseException, message: str, session: SessionState | None
) -> None:
    """Report a hidden failure with bounded session context when available."""
    if session is None:
        _report_session_failure(error, message, suppress_handler_errors=True)
        return
    close_code = session.close_code
    if type(close_code) is not int or not 0 <= close_code <= MAX_CLOSE_CODE:
        close_code = None
    with contextlib.suppress(Exception):
        details: dict[str, object] = {"message": message, "exception": error}
        details.update({
            "operation": "cleanup",
            "phase": session.phase.value,
            "close_code": close_code,
            "exception_type": type(error).__name__,
        })
        message = f"{message} (phase={session.phase.value}, close_code={close_code})"
        details["message"] = message
        asyncio.get_running_loop().call_exception_handler(details)
