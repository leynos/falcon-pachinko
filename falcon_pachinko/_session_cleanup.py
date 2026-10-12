"""Cancellation-safe cleanup and failure policy for routed sessions."""

from __future__ import annotations

import asyncio
import dataclasses as dc
import typing as typ

from ._session import (
    SessionState,
    _report_session_failure,
    _wait_for_shielded_task,
    effective_close_code,
)
from .hooks import HookContext, HookEvent

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    import falcon

    from .hooks import HookManager
    from .protocols import WebSocketLike
    from .resource import WebSocketResource


class _Dispatch(typ.Protocol):
    """Router dispatch fields needed by disconnect cleanup."""

    req: falcon.Request
    ws: WebSocketLike


@dc.dataclass(slots=True)
class CleanupContext:
    """Inputs shared by the cancellation-safe cleanup task."""

    resource: WebSocketResource
    dispatch: _Dispatch
    hook_manager: HookManager
    session: SessionState
    params: dict[str, object]
    primary_error: BaseException | None


async def run_shielded_cleanup(
    context: CleanupContext,
) -> tuple[tuple[BaseException, ...], tuple[asyncio.CancelledError, ...]]:
    """Finish one cleanup task even if its caller receives more cancellation."""
    cleanup_task = asyncio.create_task(_collect_cleanup_failures(context))
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
    context: CleanupContext,
) -> tuple[BaseException, ...]:
    """Keep unexpected housekeeping errors within the session failure policy."""
    # A cancelled shield may independently report an inner task's exception.
    # Return errors as values so our precedence policy owns their single report.
    try:
        return await _cleanup_session(context)
    except BaseException as exc:  # ruff: ignore[blind-except] - retain cleanup task failures
        return (exc,)


async def _cleanup_session(context: CleanupContext) -> tuple[BaseException, ...]:
    """Close best-effort, notify both cleanup layers, then mark the session."""
    session = context.session
    if not session.begin_disconnect():
        return ()

    failures: list[BaseException] = []
    try:
        close_code = await _close_failed_session(context, failures)
        failures.extend(await _run_disconnect_hooks(context, close_code))
    finally:
        session.mark_closed()

    return tuple(failures)


async def _close_failed_session(
    context: CleanupContext, failures: list[BaseException]
) -> int:
    """Choose the effective close code and close a failed session best-effort."""
    primary_error = context.primary_error
    recorded_code = getattr(context.dispatch.ws, "close_code", None)
    close_code = effective_close_code(recorded_code, context.session, primary_error)
    context.session.close_code = close_code
    if primary_error is not None and not bool(
        getattr(context.dispatch.ws, "closed", False)
    ):
        try:
            await context.dispatch.ws.close(close_code)
        except BaseException as exc:  # ruff: ignore[blind-except] - cleanup must continue
            failures.append(exc)
    return close_code


async def _run_disconnect_hooks(
    context: CleanupContext, close_code: int
) -> tuple[BaseException, ...]:
    """Attempt both cleanup hooks and preserve every hook failure."""
    dispatch = context.dispatch
    hook_context = HookContext(
        event=HookEvent.BEFORE_DISCONNECT,
        target=context.resource,
        resource=None,
        req=dispatch.req,
        ws=dispatch.ws,
        params=context.params,
        close_code=close_code,
        error=context.primary_error,
    )
    failures: list[BaseException] = []
    await _attempt_cleanup(
        lambda: context.hook_manager.notify_before_disconnect(hook_context),
        failures,
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
) -> None:
    """Apply primary-first exception precedence after cleanup has completed."""
    if primary_error is not None:
        _raise_primary_session_error(primary_error, cleanup_failures, interruptions)

    cancellation = _session_cancellation(primary_error, cleanup_failures, interruptions)
    if cancellation is not None:
        _report_cancelled_session_failures(None, cleanup_failures, cancellation)
        raise cancellation

    _raise_cleanup_failures(cleanup_failures)


def _raise_primary_session_error(
    primary_error: BaseException,
    cleanup_failures: tuple[BaseException, ...],
    interruptions: tuple[asyncio.CancelledError, ...],
) -> typ.NoReturn:
    """Keep the original session error ahead of every finalization failure."""
    if isinstance(primary_error, asyncio.CancelledError):
        _report_cancelled_session_failures(None, cleanup_failures, primary_error)
        raise primary_error
    for failure in cleanup_failures:
        _report_session_failure(
            failure,
            "WebSocket finalization failed after a session error",
            suppress_handler_errors=True,
        )
    for interruption in interruptions:
        _report_session_failure(
            interruption,
            "WebSocket finalization was cancelled after a session error",
            suppress_handler_errors=True,
        )
    raise primary_error


def _raise_cleanup_failures(cleanup_failures: tuple[BaseException, ...]) -> None:
    """Propagate one cleanup error directly or group multiple failures."""
    failures = list(cleanup_failures)
    if not failures:
        return
    if len(failures) == 1:
        raise failures[0]
    if all(isinstance(failure, Exception) for failure in failures):
        exceptions = [typ.cast("Exception", failure) for failure in failures]
        raise ExceptionGroup("session", exceptions)
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
) -> None:
    """Report failures hidden by cancellation through the running loop."""
    if primary_error is not None and primary_error is not cancellation:
        _report_session_failure(
            primary_error,
            "A WebSocket session failure was superseded by cancellation",
            suppress_handler_errors=True,
        )
    for failure in cleanup_failures:
        if not isinstance(failure, asyncio.CancelledError):
            _report_session_failure(
                failure,
                "WebSocket disconnect cleanup failed during cancellation",
                suppress_handler_errors=True,
            )
