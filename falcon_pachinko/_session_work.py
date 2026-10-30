"""Run ordered setup and receive work for a routed WebSocket session."""

from __future__ import annotations

import typing as typ

import falcon

from ._disconnect import _PeerDisconnectedError
from ._session import ABNORMAL_CLOSURE, SessionPhase, SessionState
from ._session_observation import (
    _log_session_observation,
    _outcome_for_error,
    _SessionObservation,
)
from ._session_setup import ConnectionSetup
from ._session_socket import receive_frame

if typ.TYPE_CHECKING:
    from ._session_driver import _SessionExecution
    from ._session_setup import SessionDispatch
    from .hooks import HookManager
    from .protocols import WebSocketLike
    from .resource import WebSocketResource


async def _run_setup_and_receive(
    execution: _SessionExecution,
    *,
    receive_loop: bool,
) -> bool:
    """Run setup, receiving only when configured, and report setup completion."""
    setup_completed = await _run_setup(execution)
    if not setup_completed:
        return setup_completed
    if not receive_loop:
        return setup_completed
    if not execution.session.can_receive:
        return setup_completed
    await _run_receive(execution)
    return setup_completed


async def _run_setup(execution: _SessionExecution) -> bool:
    """Run ordered setup and report whether receive processing may follow."""
    resource = execution.resource
    dispatch = execution.dispatch
    session = execution.session
    setup_started = execution.observation_clock()
    _log_session_observation(
        execution, _SessionObservation("setup", "started", setup_started)
    )
    try:
        setup = ConnectionSetup(
            hook_manager=execution.hook_manager,
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
                _SessionObservation(
                    "acceptance", "completed", execution.observation_clock()
                ),
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
                _SessionObservation(
                    "acceptance", "completed", execution.observation_clock()
                ),
            )
        if session.phase is SessionPhase.ESTABLISHED:
            _record_and_log_session_ending(execution, "setup", setup_started, exc)
            return False
        _log_session_failure(execution, "setup", setup_started, exc)
        raise

    return True


async def _run_receive(execution: _SessionExecution) -> None:
    """Drive receive processing and record its established-session ending."""
    receive_started = execution.observation_clock()
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
    _log_session_observation(
        execution,
        _SessionObservation(operation, _outcome_for_error(error), started, error=error),
    )


def _record_and_log_session_ending(
    execution: _SessionExecution,
    operation: str,
    started: float,
    error: BaseException,
) -> bool:
    """Record an established session's ending and report its effective outcome."""
    execution.primary_error = _record_session_ending(execution.session, error)
    _log_session_observation(
        execution,
        _SessionObservation(
            operation,
            _outcome_for_error(execution.primary_error),
            started,
            error=execution.primary_error,
        ),
    )
    return execution.session.phase is SessionPhase.ESTABLISHED


def _establish_after_resource_acceptance(
    session: SessionState, ws: WebSocketLike
) -> None:
    """Honour resource-owned acceptance even when its setup then fails."""
    if session.phase is SessionPhase.CONNECTING and _is_websocket_accepted(ws):
        session.establish()


async def _receive_session(
    resource: WebSocketResource,
    dispatch: SessionDispatch,
    session: SessionState,
    hook_manager: HookManager,
) -> None:
    """Receive and route frames until Falcon reports a disconnect."""
    if not _is_websocket_accepted(dispatch.ws):
        # An approved handshake that never completed has no close-frame channel.
        session.close_code = ABNORMAL_CLOSURE
        return
    while session.can_receive:
        try:
            raw = await receive_frame(dispatch.ws)
        except (falcon.WebSocketDisconnected, _PeerDisconnectedError) as exc:
            _record_session_disconnect_code(session, exc)
            return

        # A close can race a pending receive; discard its late frame.
        if not session.can_receive:
            return
        # A subclass may bypass the resource guard, so also check hook state.
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
        _record_session_disconnect_code(session, error)
        return None
    session.primary_error = error
    return error


def _record_session_disconnect_code(
    session: SessionState, error: BaseException
) -> None:
    """Keep an earlier local close selection ahead of disconnect metadata."""
    if session.close_code is None:
        session.close_code = _effective_disconnect_code(error)
