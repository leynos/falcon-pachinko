"""Drive the WebSocket lifecycle after the router selects its resource.

The router awaits ``run_session`` to preserve setup ordering, receive dispatch,
and exactly-once disconnect cleanup. Session state is shared with the bound
hook manager, while shielded cleanup preserves failure and cancellation policy.
"""

from __future__ import annotations

import dataclasses as dc
import time
import typing as typ

from . import observability as _observability
from ._session import (
    SessionPhase,
    SessionState,
)
from ._session_cleanup import (
    CleanupContext,
    raise_session_failures,
    run_shielded_cleanup,
)
from ._session_instrumentation import (
    _drive_session_with_observation,
    _release_and_observe_session,
    _start_session_span,
)
from ._session_observation import (
    _cleanup_outcome,
    _log_session_observation,
    _SessionObservation,
)
from ._session_socket import track_session_socket
from ._session_work import (
    _is_websocket_accepted,
    _log_session_failure,
    _run_setup_and_receive,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from ._session_setup import SessionDispatch
    from .hooks import HookContext, HookManager
    from .resource import WebSocketResource


@dc.dataclass(frozen=True, slots=True)
class _SessionOptions:
    """Bundle receive ownership and diagnostic timing choices."""

    receive_loop: bool
    observation_clock: cabc.Callable[[], float] = time.monotonic
    instrumentation_clock: cabc.Callable[[], float] = time.monotonic
    instrumentation: _observability.SessionInstrumentation = (
        _observability.NOOP_SESSION_INSTRUMENTATION
    )


@dc.dataclass(slots=True)
class _SessionExecution:
    """Keep the session's request, resource, and failure state together."""

    resource: WebSocketResource
    dispatch: SessionDispatch
    session: SessionState
    hook_manager: HookManager
    observation_clock: cabc.Callable[[], float]
    instrumentation_clock: cabc.Callable[[], float]
    instrumentation: _observability.SessionInstrumentation
    context: HookContext | None = None
    primary_error: BaseException | None = None
    cleanup_failures: tuple[BaseException, ...] = ()


@dc.dataclass(slots=True)
class _SessionRun:
    """Keep timing, span, and terminal state for one router-owned attempt."""

    execution: _SessionExecution
    options: _SessionOptions
    started: float
    instrumentation_started: float
    session_span: _observability._SpanOwner
    handed_off_to_application: bool = False
    error: BaseException | None = None
    outcome: _observability.Outcome | None = None


async def run_session(
    resource: WebSocketResource,
    dispatch: SessionDispatch,
    *,
    hook_manager: HookManager,
    options: _SessionOptions,
) -> bool:
    """Run one connection's setup, receive loop, and cleanup.

    The observation clock controls diagnostic durations independently of
    session state, so tests can assert timings without a real clock.

    Parameters
    ----------
    resource:
        Resource selected for this connection.
    dispatch:
        Request, socket, and route match owned by the router.
    hook_manager:
        Hook manager bound to the selected resource chain.
    options:
        Receive-loop ownership and diagnostic clock for this session.

    Returns
    -------
    bool
        ``True`` after this matched route handles the connection attempt.
    """
    run = _start_session_run(resource, dispatch, hook_manager, options)
    try:
        await _drive_session_with_observation(run, _drive_session)
        return True
    finally:
        _release_and_observe_session(run, _release_session_ownership)


def _start_session_run(
    resource: WebSocketResource,
    dispatch: SessionDispatch,
    hook_manager: HookManager,
    options: _SessionOptions,
) -> _SessionRun:
    """Bind session guards and start timing before setup invokes user code."""
    execution = _prepare_session_execution(resource, dispatch, hook_manager, options)
    started = options.observation_clock()
    instrumentation_started = options.instrumentation_clock()
    session_span = _start_session_span(execution, options)
    _log_session_observation(
        execution, _SessionObservation("session", "started", started)
    )
    return _SessionRun(
        execution, options, started, instrumentation_started, session_span
    )


def _prepare_session_execution(
    resource: WebSocketResource,
    dispatch: SessionDispatch,
    hook_manager: HookManager,
    options: _SessionOptions,
) -> _SessionExecution:
    """Bind receive guards before setup can invoke any resource hooks."""
    execution = _SessionExecution(
        resource,
        dispatch,
        SessionState(),
        hook_manager,
        observation_clock=options.observation_clock,
        instrumentation_clock=options.instrumentation_clock,
        instrumentation=options.instrumentation,
    )
    dispatch.session = execution.session
    dispatch.ws = track_session_socket(dispatch.ws, execution.session)
    hook_manager.bind_session(execution.session)
    return execution


async def _drive_session(
    execution: _SessionExecution,
    options: _SessionOptions,
    started: float,
) -> bool:
    """Run setup and cleanup, returning whether receive ownership was handed off."""
    try:
        setup_completed = await _run_setup_and_receive(
            execution, receive_loop=options.receive_loop
        )
        if _should_finish_established_session(
            execution,
            receive_loop=options.receive_loop,
            setup_completed=setup_completed,
        ):
            await _finish_established_session(execution)
    except BaseException as exc:
        _log_session_failure(execution, "session", started, exc)
        raise

    outcome = (
        "rejected"
        if execution.session.phase is SessionPhase.CONNECTING
        else "completed"
    )
    _log_session_observation(
        execution, _SessionObservation("session", outcome, started)
    )
    return _should_handoff_setup_session(execution, receive_loop=options.receive_loop)


def _release_session_ownership(
    execution: _SessionExecution,
    options: _SessionOptions,
    *,
    handed_off_to_application: bool,
) -> None:
    """Release setup-only guards without weakening disconnect ownership."""
    if handed_off_to_application:
        # The router's receive guard must not outlive its setup-only lease.
        execution.hook_manager.release_session(execution.session)
        execution.session.mark_closed()
    elif _should_release_setup_session(execution, receive_loop=options.receive_loop):
        # Rejections and pre-approval failures must not leave stale state.
        execution.session.mark_closed()


def _should_finish_established_session(
    execution: _SessionExecution,
    *,
    receive_loop: bool,
    setup_completed: bool,
) -> bool:
    """Finish approved sessions when setup cannot hand off a live loop."""
    session = execution.session
    if session.phase is not SessionPhase.ESTABLISHED:
        return False
    if not _is_websocket_accepted(execution.dispatch.ws):
        return True
    if receive_loop or not setup_completed:
        return True
    return bool(getattr(execution.dispatch.ws, "closed", False))


def _should_release_setup_session(
    execution: _SessionExecution,
    *,
    receive_loop: bool,
) -> bool:
    """Release ownership for rejected or setup-only router calls."""
    phase = execution.session.phase
    if phase is SessionPhase.CONNECTING:
        return True
    if receive_loop:
        return False
    return phase is SessionPhase.ESTABLISHED


def _should_handoff_setup_session(
    execution: _SessionExecution,
    *,
    receive_loop: bool,
) -> bool:
    """Release receive hooks only after successful setup-only ownership transfer."""
    return not receive_loop and execution.session.phase is SessionPhase.ESTABLISHED


async def _finish_established_session(
    execution: _SessionExecution,
) -> None:
    """Complete shielded cleanup with setup params and ordered failures."""
    dispatch = execution.dispatch
    context = execution.context
    params = (
        context.params
        if context is not None and context.params is not None
        else dict(dispatch.match.params)
    )
    cleanup_context = CleanupContext(
        resource=execution.resource,
        dispatch=dispatch,
        hook_manager=execution.hook_manager,
        session=execution.session,
        params=dict(params),
        primary_error=execution.primary_error,
        instrumentation=execution.instrumentation,
        instrumentation_clock=execution.instrumentation_clock,
    )
    cleanup_started = execution.observation_clock()
    _log_session_observation(
        execution, _SessionObservation("cleanup", "started", cleanup_started)
    )
    cleanup_failures, interrupted = await run_shielded_cleanup(cleanup_context)
    execution.cleanup_failures = cleanup_failures
    _log_session_observation(
        execution,
        _SessionObservation(
            "cleanup",
            _cleanup_outcome(cleanup_failures, interrupted),
            cleanup_started,
            error=cleanup_failures[0] if cleanup_failures else None,
            cleanup_failures=cleanup_failures,
        ),
    )
    raise_session_failures(
        execution.primary_error,
        cleanup_failures,
        interrupted,
        session=execution.session,
    )
