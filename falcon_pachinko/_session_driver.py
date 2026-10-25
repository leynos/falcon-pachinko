"""Drive the WebSocket lifecycle after the router selects its resource.

The router awaits ``run_session`` to preserve setup ordering, receive dispatch,
and exactly-once disconnect cleanup. Session state is shared with the bound
hook manager, while shielded cleanup preserves failure and cancellation policy.
"""

from __future__ import annotations

import abc
import asyncio
import contextlib
import dataclasses as dc
import logging
import time
import typing as typ

import falcon

from ._disconnect import _PeerDisconnectedError
from ._session import (
    ABNORMAL_CLOSURE,
    INTERNAL_ERROR,
    MAX_CLOSE_CODE,
    SessionPhase,
    SessionState,
)
from ._session_cleanup import (
    CleanupContext,
    raise_session_failures,
    run_shielded_cleanup,
)
from ._session_setup import ConnectionSetup
from ._session_socket import receive_frame, track_session_socket
from .diagnostics import _type_name

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from .hooks import HookContext, HookManager
    from .protocols import WebSocketLike
    from .resource import WebSocketResource

logger = logging.getLogger(__name__)
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


class SessionRouteMatch(typ.Protocol):
    """Route captures read by session setup without depending on router internals."""

    @property
    @abc.abstractmethod
    def params(self) -> cabc.Mapping[str, str]:
        """Captured URI parameters for the selected resource."""


class SessionDispatch(typ.Protocol):
    """Mutable socket and lifecycle fields supplied by the routing pipeline."""

    req: falcon.Request
    ws: WebSocketLike
    session: SessionState | None

    @property
    @abc.abstractmethod
    def match(self) -> SessionRouteMatch:
        """Selected route parameters consumed by the session driver."""


@dc.dataclass(slots=True)
class _SessionExecution:
    """Keep the session's request, resource, and failure state together."""

    resource: WebSocketResource
    dispatch: SessionDispatch
    session: SessionState
    hook_manager: HookManager
    context: HookContext | None = None
    primary_error: BaseException | None = None


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
            "duration_seconds": max(0.0, time.monotonic() - observation.started),
            "resource_type": _type_name(execution.resource),
            "exception_type": (
                _type_name(observation.error) if observation.error is not None else None
            ),
            "primary_exception_type": (
                _type_name(execution.primary_error)
                if execution.primary_error is not None
                else None
            ),
            "cleanup_failure_count": min(
                len(observation.cleanup_failures), _MAX_CLEANUP_FAILURE_TYPES
            ),
            "cleanup_failures_truncated": (
                len(observation.cleanup_failures) > _MAX_CLEANUP_FAILURE_TYPES
            ),
            "cleanup_failure_types": failure_types,
        },
    )


async def run_session(
    resource: WebSocketResource,
    dispatch: SessionDispatch,
    *,
    hook_manager: HookManager,
) -> bool:
    """Run setup, receive frames, and finish the accepted session."""
    execution = _SessionExecution(resource, dispatch, SessionState(), hook_manager)
    dispatch.session = execution.session
    dispatch.ws = track_session_socket(dispatch.ws, execution.session)
    hook_manager.bind_session(execution.session)
    started = time.monotonic()
    _log_session_observation(
        execution, _SessionObservation("session", "started", started)
    )
    try:
        await _run_setup_and_receive(execution)

        if execution.session.phase is SessionPhase.ESTABLISHED:
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

    return True


async def _run_setup_and_receive(
    execution: _SessionExecution,
) -> None:
    """Run setup first, then receive only while the accepted session is live."""
    if await _run_setup(execution) and execution.session.can_receive:
        await _run_receive(execution)


async def _run_setup(execution: _SessionExecution) -> bool:
    """Run ordered setup and report whether receive processing may follow."""
    resource = execution.resource
    dispatch = execution.dispatch
    session = execution.session
    hook_manager = execution.hook_manager
    setup_started = time.monotonic()
    _log_session_observation(
        execution, _SessionObservation("setup", "started", setup_started)
    )
    try:
        setup = ConnectionSetup(
            hook_manager=hook_manager,
            resource=resource,
            req=dispatch.req,
            ws=dispatch.ws,
            route_params=dispatch.match.params,
            is_accepted=_is_websocket_accepted,
            is_unaccepted=_is_websocket_unaccepted,
            on_approval=session.establish,
        )
        execution.context = await setup.prepare()
        await setup.execute()
        await setup.finalize()
        if session.phase is SessionPhase.ESTABLISHED and _is_websocket_accepted(
            dispatch.ws
        ):
            _log_session_observation(
                execution,
                _SessionObservation("acceptance", "completed", time.monotonic()),
            )
        setup_outcome = (
            "rejected" if session.phase is SessionPhase.CONNECTING else "completed"
        )
        _log_session_observation(
            execution, _SessionObservation("setup", setup_outcome, setup_started)
        )
    except BaseException as exc:
        was_connecting = session.phase is SessionPhase.CONNECTING
        _establish_after_resource_acceptance(session, dispatch.ws)
        if was_connecting and session.phase is SessionPhase.ESTABLISHED:
            _log_session_observation(
                execution,
                _SessionObservation("acceptance", "completed", time.monotonic()),
            )
        if session.phase is SessionPhase.ESTABLISHED:
            _record_and_log_session_ending(execution, "setup", setup_started, exc)
            return False
        _log_session_failure(execution, "setup", setup_started, exc)
        raise

    return True


async def _run_receive(execution: _SessionExecution) -> None:
    """Drive receive processing and record its established-session ending."""
    receive_started = time.monotonic()
    _log_session_observation(
        execution, _SessionObservation("receive", "started", receive_started)
    )
    try:
        await _receive_session(
            execution.resource,
            execution.dispatch,
            execution.session,
            execution.hook_manager,
        )
    except BaseException as exc:
        session_remains_established = _record_and_log_session_ending(
            execution, "receive", receive_started, exc
        )
        if session_remains_established:
            return
        raise
    _log_session_observation(
        execution, _SessionObservation("receive", "completed", receive_started)
    )


def _log_session_failure(
    execution: _SessionExecution,
    operation: str,
    started: float,
    error: BaseException,
) -> None:
    """Keep failure observations consistent before their original re-raise."""
    outcome = "cancelled" if isinstance(error, asyncio.CancelledError) else "failed"
    _log_session_observation(
        execution, _SessionObservation(operation, outcome, started, error=error)
    )


def _record_and_log_session_ending(
    execution: _SessionExecution,
    operation: str,
    started: float,
    error: BaseException,
) -> bool:
    """Record an established session's ending and report its effective outcome."""
    execution.primary_error = _record_session_ending(execution.session, error)
    outcome = (
        "completed"
        if execution.primary_error is None
        else (
            "cancelled"
            if isinstance(execution.primary_error, asyncio.CancelledError)
            else "failed"
        )
    )
    _log_session_observation(
        execution,
        _SessionObservation(operation, outcome, started, error=execution.primary_error),
    )
    return execution.session.phase is SessionPhase.ESTABLISHED


def _establish_after_resource_acceptance(
    session: SessionState, ws: WebSocketLike
) -> None:
    """Honour resource-owned acceptance even when its setup then fails."""
    if session.phase is SessionPhase.CONNECTING and _is_websocket_accepted(ws):
        session.establish()


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
    )
    cleanup_started = time.monotonic()
    _log_session_observation(
        execution, _SessionObservation("cleanup", "started", cleanup_started)
    )
    cleanup_failures, interrupted = await run_shielded_cleanup(cleanup_context)
    cleanup_outcome = (
        "failed" if cleanup_failures else "cancelled" if interrupted else "completed"
    )
    _log_session_observation(
        execution,
        _SessionObservation(
            "cleanup",
            cleanup_outcome,
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


async def _receive_session(
    resource: WebSocketResource,
    dispatch: SessionDispatch,
    session: SessionState,
    hook_manager: HookManager,
) -> None:
    """Receive and route frames until Falcon reports a disconnect."""
    if not _is_websocket_accepted(dispatch.ws):
        session.close_code = INTERNAL_ERROR
        with contextlib.suppress(Exception):
            await dispatch.ws.close(INTERNAL_ERROR)
        return
    while session.can_receive:
        try:
            raw = await receive_frame(dispatch.ws)
        except (falcon.WebSocketDisconnected, _PeerDisconnectedError) as exc:
            session.close_code = _effective_disconnect_code(exc)
            return

        # A close can be initiated by another task while receive_media is
        # suspended. Do not dispatch the late frame into receive hooks.
        if not session.can_receive:
            return
        # An overridden dispatcher may bypass the resource's guard. Drop
        # late buffered frames here while retaining the disconnect code.
        if not hook_manager.can_receive(dispatch.ws):
            continue
        await resource.dispatch(dispatch.ws, raw)


def _is_websocket_accepted(ws: WebSocketLike) -> bool:
    """Probe Falcon's public acceptance state without changing the protocol."""
    try:
        unaccepted = typ.cast("typ.Any", ws).unaccepted
    except AttributeError:
        return False
    return unaccepted is False


def _is_websocket_unaccepted(ws: WebSocketLike) -> bool:
    """Treat missing Falcon acceptance state as not yet accepted."""
    return not _is_websocket_accepted(ws)


def _effective_disconnect_code(error: BaseException) -> int:
    """Return the effective close code, using abnormal closure when absent."""
    close_code = getattr(error, "code", None)
    return close_code if isinstance(close_code, int) else ABNORMAL_CLOSURE


def _record_session_ending(
    session: SessionState, error: BaseException
) -> BaseException | None:
    """Distinguish a transport ending from an unexpected session failure."""
    if isinstance(error, (falcon.WebSocketDisconnected, _PeerDisconnectedError)):
        session.close_code = _effective_disconnect_code(error)
        return None
    session.primary_error = error
    return error
