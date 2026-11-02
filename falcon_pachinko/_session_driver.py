"""Drive the WebSocket lifecycle after the router selects its resource.

The router awaits ``run_session`` to preserve setup ordering, receive dispatch,
and exactly-once disconnect cleanup. Session state is shared with the bound
hook manager, while shielded cleanup preserves failure and cancellation policy.
"""

from __future__ import annotations

import asyncio
import contextlib
import typing as typ

from ._disconnect import _PeerDisconnectedError
from ._observability import _LifecycleOptions, _observe_lifecycle
from ._session import (
    ABNORMAL_CLOSURE,
    INTERNAL_ERROR,
    effective_close_code,
)
from ._session_cleanup import (
    raise_session_failures,
    run_shielded_cleanup,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from ._observability import _LifecycleObservation
    from ._session import SessionState
    from ._session_context import SessionDriverContext
    from .observability import (
        LifecycleFailureCategory,
        LifecycleOperation,
        LifecycleSpanName,
    )
    from .protocols import DisconnectClassifier


@contextlib.contextmanager
def _observe_session_operation(
    context: SessionDriverContext,
    operation: LifecycleOperation,
    *,
    span_name: LifecycleSpanName | None = None,
    failure_category: LifecycleFailureCategory = "other",
) -> cabc.Iterator[_LifecycleObservation]:
    """Capture the final close category before completing a session observation.

    Yields
    ------
    _LifecycleObservation
        State for operation-specific failure classification.
    """
    with _observe_lifecycle(
        context.observer,
        operation,
        options=_LifecycleOptions(
            span_name=span_name,
            failure_category=failure_category,
            monotonic_clock=context.monotonic_clock,
        ),
    ) as observation:
        try:
            yield observation
        finally:
            observation.set_close_code(context.session_state.close_code)


async def run_session(
    context: SessionDriverContext,
    finalize: cabc.Callable[
        [BaseException | None, BaseException | None], cabc.Awaitable[None]
    ],
    *,
    receive_loop: bool = True,
) -> bool:
    """Run post-accept setup and either own cleanup or hand off the socket.

    Returns
    -------
    bool
        Whether successful startup transferred later receive and cleanup
        ownership to the application.
    """
    primary_error: BaseException | None = None
    finalization_error: BaseException | None = None
    handed_off = False

    try:
        handed_off = await _run_session_body(context, receive_loop=receive_loop)
    except asyncio.CancelledError as exc:
        primary_error, finalization_error = await _handle_session_failure(context, exc)
    except Exception as exc:  # ruff: ignore[blind-except] - hooks are extensible
        primary_error, finalization_error = await _handle_session_failure(context, exc)
    finally:
        if not handed_off:
            finalization_error = await _close_after_session_run(
                context, finalization_error
            )
            await finalize(primary_error, finalization_error)
    return handed_off


async def _run_session_body(
    context: SessionDriverContext, *, receive_loop: bool
) -> bool:
    """Run resource setup, then receive frames or transfer their ownership."""
    with _observe_session_operation(
        context, "session_start", failure_category="application"
    ):
        await context.resource.on_session_start(context.websocket)
    if not context.hook_manager.can_receive(context.websocket):
        return False
    if receive_loop:
        await _receive_session(context)
        return False

    context.hook_manager.release_session(context.session_state)
    context.session_state.mark_closed()
    return True


async def _close_after_session_run(
    context: SessionDriverContext,
    finalization_error: BaseException | None,
) -> BaseException | None:
    """Close an open transport and retain the first finalization failure."""
    if isinstance(context.session_state.primary_error, asyncio.CancelledError) or bool(
        getattr(context.websocket, "closed", False)
    ):
        return finalization_error
    close_code = (
        context.session_state.close_code
        if context.session_state.close_code is not None
        else 1000
    )
    try:
        await context.websocket.close(close_code)
    except asyncio.CancelledError as close_error:
        return finalization_error or close_error
    except Exception as close_error:  # ruff: ignore[blind-except] - adapter close
        return finalization_error or close_error
    return finalization_error


async def _close_after_session_error(
    context: SessionDriverContext, error: BaseException | None
) -> BaseException | None:
    """Close failed sessions with the code associated with their cause."""
    if error is None:
        return None
    if isinstance(error, asyncio.CancelledError):
        return await _close_cancelled_session(context, error)
    if context.websocket.closed:
        context.session_state.close_code = effective_close_code(
            context.websocket.close_code, context.session_state, None
        )
        return None
    close_code = INTERNAL_ERROR
    if context.session_state.close_code is None:
        context.session_state.close_code = close_code
    return await _attempt_session_close(context, close_code)


async def _close_cancelled_session(
    context: SessionDriverContext, error: asyncio.CancelledError
) -> BaseException | None:
    """Close a cancelled session with its effective cancellation code."""
    close_code = effective_close_code(
        context.websocket.close_code, context.session_state, error
    )
    context.session_state.close_code = close_code
    if context.websocket.closed:
        return None
    return await _attempt_session_close(context, close_code)


async def _attempt_session_close(
    context: SessionDriverContext, close_code: int
) -> BaseException | None:
    """Return a close failure so the session precedence policy can handle it."""
    try:
        await context.websocket.close(close_code)
    except asyncio.CancelledError as close_error:
        return close_error
    except Exception as close_error:  # ruff: ignore[blind-except] - adapter close
        return close_error
    return None


async def _handle_session_failure(
    context: SessionDriverContext, error: BaseException
) -> tuple[BaseException | None, BaseException | None]:
    """Record a session failure and close using its lifecycle code."""
    primary_error = _record_session_ending(
        context.session_state, error, context.disconnect_classifier
    )
    finalization_error = await _close_after_session_error(context, primary_error)
    return primary_error, finalization_error


async def finalize_session(context: SessionDriverContext) -> None:
    """Close if needed, run disconnect hooks, then apply error precedence."""
    with _observe_session_operation(
        context,
        "finalization",
        span_name="falcon_pachinko.websocket.finalization",
        failure_category="cleanup",
    ) as observation:
        await _finalize_session(context, observation)


async def _finalize_session(
    context: SessionDriverContext, observation: _LifecycleObservation
) -> None:
    """Retain the finalization policy while reporting its selected failure."""
    session = context.session_state
    recorded_code = getattr(context.websocket, "close_code", None)
    session.close_code = effective_close_code(
        recorded_code, session, context.primary_error
    )

    cleanup_failures, interrupted = await run_shielded_cleanup(context)
    failures = (
        (context.finalization_error,) if context.finalization_error is not None else ()
    ) + cleanup_failures
    if context.primary_error is not None:
        observation.record_failure(context.primary_error, "application")
    raise_session_failures(
        context.primary_error,
        failures,
        interrupted,
        session=context.session_state,
    )


async def _receive_session(context: SessionDriverContext) -> None:
    """Receive and route frames until the transport reports a disconnect."""
    with _observe_session_operation(
        context, "receive_termination", failure_category="transport"
    ) as observation:
        await _receive_session_body(context, observation)


async def _receive_session_body(
    context: SessionDriverContext, observation: _LifecycleObservation
) -> None:
    """Dispatch frames silently and classify the one receive-loop termination."""
    resource = context.resource
    dispatch = context.dispatch
    session = context.session_state
    hook_manager = context.hook_manager
    while hook_manager.can_receive(dispatch.ws):
        try:
            raw = await context.frame_receiver(dispatch.ws)
        except _PeerDisconnectedError as exc:
            session.peer_disconnected = True
            if session.close_code is None:
                session.close_code = _effective_disconnect_code(exc)
            return

        # A close can be initiated by another task while receive_media is
        # suspended. Do not dispatch the late frame into receive hooks.
        if not hook_manager.can_receive(dispatch.ws):
            return
        try:
            await resource.dispatch(dispatch.ws, raw)
        except BaseException as error:
            observation.record_failure(error, "application")
            raise


def _effective_disconnect_code(error: BaseException) -> int:
    """Return the effective close code, using abnormal closure when absent."""
    close_code = getattr(error, "code", None)
    return close_code if isinstance(close_code, int) else ABNORMAL_CLOSURE


def _record_session_ending(
    session: SessionState,
    error: BaseException,
    disconnect_classifier: DisconnectClassifier,
) -> BaseException | None:
    """Distinguish a transport ending from an unexpected session failure."""
    if (disconnect := disconnect_classifier(error)) is not None:
        session.peer_disconnected = True
        if session.close_code is None:
            session.close_code = _effective_disconnect_code(disconnect)
        return None
    session.primary_error = error
    return error
