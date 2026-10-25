"""Cancellation-safe cleanup and failure policy for routed sessions."""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses as dc
import typing as typ

from ._session import (
    ABNORMAL_CLOSURE,
    INTERNAL_ERROR,
    MAX_CLOSE_CODE,
    SessionState,
)
from .diagnostics import _type_name
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
    interruptions = await _join_cleanup_task(cleanup_task)
    return _cleanup_task_failures(cleanup_task), interruptions


async def _join_cleanup_task(
    cleanup_task: asyncio.Task[tuple[BaseException, ...]],
) -> tuple[asyncio.CancelledError, ...]:
    """Keep joining the owned cleanup task through repeated cancellation."""
    interruptions: list[asyncio.CancelledError] = []
    try:
        await asyncio.shield(cleanup_task)
    except asyncio.CancelledError as exc:
        interruptions.append(exc)
        interruptions.extend(await _join_after_cancellation(cleanup_task))
    return tuple(interruptions)


async def _join_after_cancellation(
    cleanup_task: asyncio.Task[tuple[BaseException, ...]],
) -> tuple[asyncio.CancelledError, ...]:
    """Finish the same cleanup task without swallowing later interruptions."""
    interruptions: list[asyncio.CancelledError] = []
    while not cleanup_task.done():
        try:
            await asyncio.shield(cleanup_task)
        except asyncio.CancelledError as exc:
            interruptions.append(exc)
    return tuple(interruptions)


def _cleanup_task_failures(
    cleanup_task: asyncio.Task[tuple[BaseException, ...]],
) -> tuple[BaseException, ...]:
    """Return task failures as values for the session precedence policy."""
    if cleanup_task.cancelled():
        return (asyncio.CancelledError(),)
    try:
        return cleanup_task.result()
    except Exception as exc:  # ruff: ignore[blind-except] - retain task failures
        return (exc,)
    except asyncio.CancelledError as exc:
        return (exc,)


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
    if primary_error is None:
        close_code = context.session.close_code
        if close_code is None:
            close_code = ABNORMAL_CLOSURE
        context.session.close_code = close_code
        return close_code

    is_cancelled = isinstance(primary_error, asyncio.CancelledError)
    is_accepted = getattr(context.dispatch.ws, "unaccepted", None) is False
    close_code = ABNORMAL_CLOSURE if is_cancelled or not is_accepted else INTERNAL_ERROR
    context.session.close_code = close_code
    if is_cancelled or not is_accepted:
        return close_code
    try:
        with contextlib.suppress(Exception):
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
    *,
    session: SessionState | None = None,
) -> None:
    """Apply primary-first exception precedence after cleanup has completed."""
    cancellation = _session_cancellation(primary_error, cleanup_failures, interruptions)
    if cancellation is not None:
        _raise_cancelled_session(primary_error, cleanup_failures, cancellation, session)
    failures = _combined_session_failures(primary_error, cleanup_failures)
    _raise_session_failure_group(primary_error, cleanup_failures, failures, session)


def _combined_session_failures(
    primary_error: BaseException | None,
    cleanup_failures: tuple[BaseException, ...],
) -> list[BaseException]:
    """Preserve the primary error before every cleanup failure."""
    primary = [primary_error] if primary_error is not None else []
    return primary + list(cleanup_failures)


def _raise_cancelled_session(
    primary_error: BaseException | None,
    cleanup_failures: tuple[BaseException, ...],
    cancellation: asyncio.CancelledError,
    session: SessionState | None,
) -> typ.NoReturn:
    """Keep cancellation bare and report failures hidden by its precedence."""
    _report_cancelled_session_failures(
        primary_error, cleanup_failures, cancellation, session
    )
    raise cancellation


def _raise_session_failure_group(
    primary_error: BaseException | None,
    cleanup_failures: tuple[BaseException, ...],
    failures: list[BaseException],
    session: SessionState | None,
) -> None:
    """Raise one failure unchanged or group the ordered failure set."""
    if not failures:
        return
    if len(failures) == 1:
        raise failures[0]
    _raise_multiple_session_failures(primary_error, cleanup_failures, failures, session)


def _raise_multiple_session_failures(
    primary_error: BaseException | None,
    cleanup_failures: tuple[BaseException, ...],
    failures: list[BaseException],
    session: SessionState | None,
) -> typ.NoReturn:
    """Use the narrowest exception group that preserves session precedence."""
    if _are_ordinary_failures(failures):
        exceptions = [typ.cast("Exception", failure) for failure in failures]
        raise ExceptionGroup("session", exceptions)
    if _preserves_primary_base_exception(primary_error):
        _report_base_exception_cleanup_failures(cleanup_failures, session)
        raise typ.cast("BaseException", primary_error)
    raise BaseExceptionGroup("session", failures)


def _are_ordinary_failures(failures: list[BaseException]) -> bool:
    """Check whether ordinary exception grouping preserves all members."""
    return all(isinstance(failure, Exception) for failure in failures)


def _preserves_primary_base_exception(
    primary_error: BaseException | None,
) -> bool:
    """Keep non-cancellation base exceptions primary over cleanup failures."""
    return primary_error is not None and not isinstance(primary_error, Exception)


def _report_base_exception_cleanup_failures(
    cleanup_failures: tuple[BaseException, ...],
    session: SessionState | None,
) -> None:
    """Report cleanup failures when a non-ordinary primary must stay bare."""
    message = "WebSocket disconnect cleanup failed after a base exception"
    for failure in cleanup_failures:
        _report_cleanup_failure(failure, message, session)


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
        _report_cleanup_failure(
            primary_error,
            "A WebSocket session failure was superseded by cancellation",
            session,
        )
    for failure in cleanup_failures:
        if not isinstance(failure, asyncio.CancelledError):
            _report_cleanup_failure(
                failure,
                "WebSocket disconnect cleanup failed during cancellation",
                session,
            )


def _report_cleanup_failure(
    error: BaseException, message: str, session: SessionState | None
) -> None:
    """Report failures that cannot be grouped with a bare cancellation."""
    # Preserve application exception objects for both custom and default loop
    # handlers: type-only logging would lose failures hidden by bare cancellation.
    with contextlib.suppress(Exception):
        details: dict[str, object] = {"message": message, "exception": error}
        if session is not None:
            details.update({
                "operation": "cleanup",
                "phase": session.phase.value,
                "close_code": _safe_close_code(session.close_code),
                "exception_type": _type_name(error),
            })
            message = (
                f"{message} (phase={session.phase.value}, "
                f"close_code={_safe_close_code(session.close_code)})"
            )
            details["message"] = message
        asyncio.get_running_loop().call_exception_handler(details)


def _safe_close_code(close_code: int | None) -> int | None:
    """Keep loop-handler metadata within the WebSocket code range."""
    if type(close_code) is int and 0 <= close_code <= MAX_CLOSE_CODE:
        return close_code
    return None
