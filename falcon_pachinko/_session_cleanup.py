"""Cancellation-safe cleanup and failure policy for routed sessions."""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses as dc
import typing as typ

from ._session import ABNORMAL_CLOSURE, GOING_AWAY, INTERNAL_ERROR, SessionState
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
class _CleanupContext:
    """Inputs shared by the cancellation-safe cleanup task."""

    resource: WebSocketResource
    dispatch: _Dispatch
    hook_manager: HookManager
    session: SessionState
    params: dict[str, object]
    primary_error: BaseException | None


async def _run_shielded_cleanup(
    context: _CleanupContext,
) -> tuple[tuple[BaseException, ...], tuple[asyncio.CancelledError, ...]]:
    """Finish one cleanup task even if its caller receives more cancellation."""
    cleanup_task = asyncio.create_task(_cleanup_session(context))
    interruptions: list[asyncio.CancelledError] = []
    while True:
        try:
            await asyncio.shield(cleanup_task)
            break
        except asyncio.CancelledError as exc:
            interruptions.append(exc)
            if cleanup_task.done():
                break

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


async def _cleanup_session(context: _CleanupContext) -> tuple[BaseException, ...]:
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
    context: _CleanupContext, failures: list[BaseException]
) -> int:
    """Choose the effective close code and close a failed session best-effort."""
    primary_error = context.primary_error
    if primary_error is None:
        close_code = context.session.close_code
        if close_code is None:
            close_code = ABNORMAL_CLOSURE
        context.session.close_code = close_code
        return close_code

    close_code = (
        GOING_AWAY
        if isinstance(primary_error, asyncio.CancelledError)
        else INTERNAL_ERROR
    )
    context.session.close_code = close_code
    try:
        with contextlib.suppress(Exception):
            await context.dispatch.ws.close(close_code)
    except BaseException as exc:  # ruff: ignore[blind-except] - cleanup must continue
        failures.append(exc)
    return close_code


async def _run_disconnect_hooks(
    context: _CleanupContext, close_code: int
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


def _raise_session_failures(
    primary_error: BaseException | None,
    cleanup_failures: tuple[BaseException, ...],
    interruptions: tuple[asyncio.CancelledError, ...],
) -> None:
    """Apply primary-first exception precedence after cleanup has completed."""
    cancellation = _session_cancellation(primary_error, cleanup_failures, interruptions)
    if cancellation is not None:
        _report_cancelled_session_failures(
            primary_error, cleanup_failures, cancellation
        )
        raise cancellation

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
    if primary_error is not None and not isinstance(primary_error, Exception):
        for failure in cleanup_failures:
            _report_cleanup_failure(
                failure,
                "WebSocket disconnect cleanup failed after a base exception",
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
) -> None:
    """Report failures hidden by cancellation through the running loop."""
    if primary_error is not None and primary_error is not cancellation:
        _report_cleanup_failure(
            primary_error,
            "A WebSocket session failure was superseded by cancellation",
        )
    for failure in cleanup_failures:
        if not isinstance(failure, asyncio.CancelledError):
            _report_cleanup_failure(
                failure,
                "WebSocket disconnect cleanup failed during cancellation",
            )


def _report_cleanup_failure(error: BaseException, message: str) -> None:
    """Report failures that cannot be grouped with a bare cancellation."""
    with contextlib.suppress(Exception):
        asyncio.get_running_loop().call_exception_handler({
            "message": message,
            "exception": error,
        })
