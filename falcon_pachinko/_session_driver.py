"""Drive the WebSocket lifecycle after the router selects its resource.

The router awaits ``run_session`` to preserve setup ordering, receive dispatch,
and exactly-once disconnect cleanup. Session state is shared with the bound
hook manager, while shielded cleanup preserves failure and cancellation policy.
"""

from __future__ import annotations

import abc
import contextlib
import dataclasses as dc
import logging
import typing as typ

import falcon

from ._disconnect import _PeerDisconnectedError
from ._session import ABNORMAL_CLOSURE, INTERNAL_ERROR, SessionPhase, SessionState
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
    await _run_setup_and_receive(execution)

    if isinstance(execution.primary_error, Exception):
        logger.error(
            "WebSocket session failed: %s", _type_name(execution.primary_error)
        )

    if execution.session.phase is SessionPhase.ESTABLISHED:
        await _finish_established_session(execution)

    return True


async def _run_setup_and_receive(
    execution: _SessionExecution,
) -> None:
    """Run ordered setup and receive work, recording only established failures."""
    resource = execution.resource
    dispatch = execution.dispatch
    session = execution.session
    hook_manager = execution.hook_manager
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
        if session.can_receive:
            await _receive_session(resource, dispatch, session, hook_manager)
    except BaseException as exc:
        _establish_after_resource_acceptance(session, dispatch.ws)
        if session.phase is SessionPhase.ESTABLISHED:
            execution.primary_error = _record_session_ending(session, exc)
            return
        raise


def _establish_after_resource_acceptance(
    session: SessionState, ws: WebSocketLike
) -> None:
    """Honor resource-owned acceptance even when its setup then fails."""
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
    cleanup_failures, interrupted = await run_shielded_cleanup(cleanup_context)
    raise_session_failures(execution.primary_error, cleanup_failures, interrupted)


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
