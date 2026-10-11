"""Drive the WebSocket lifecycle after the router selects its resource.

The router awaits ``run_session`` to preserve setup ordering, receive dispatch,
and exactly-once disconnect cleanup. Session state is shared with the bound
hook manager, while shielded cleanup preserves failure and cancellation policy.
"""

from __future__ import annotations

import abc
import contextlib
import typing as typ

import falcon

from ._session import ABNORMAL_CLOSURE, INTERNAL_ERROR, SessionPhase, SessionState
from ._session_cleanup import (
    CleanupContext,
    raise_session_failures,
    run_shielded_cleanup,
)
from ._session_socket import receive_frame, track_session_socket

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from .hooks import HookContext, HookManager
    from .protocols import WebSocketLike
    from .resource import WebSocketResource


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


async def run_session(
    resource: WebSocketResource,
    dispatch: SessionDispatch,
    *,
    hook_manager: HookManager,
) -> bool:
    """Run setup, receive frames, and finish the accepted session."""
    session = SessionState()
    dispatch.session = session
    dispatch.ws = track_session_socket(dispatch.ws, session)
    hook_manager.bind_session(session)
    context: HookContext | None = None
    primary_error: BaseException | None = None

    try:
        context = await _prepare_connection_context(hook_manager, resource, dispatch)
        should_accept = await _execute_resource_handler(
            resource, dispatch, context, hook_manager
        )
        if should_accept or _is_websocket_accepted(dispatch.ws):
            session.establish()
        await _finalize_connection(
            dispatch.ws,
            should_accept=should_accept,
            context=context,
            hook_manager=hook_manager,
        )
        if session.can_receive:
            await _receive_session(resource, dispatch, session, hook_manager)
    except BaseException as exc:
        if session.phase is SessionPhase.CONNECTING and _is_websocket_accepted(
            dispatch.ws
        ):
            session.establish()
        if session.phase is SessionPhase.ESTABLISHED:
            primary_error = _record_session_ending(session, exc)
        else:
            raise

    if session.phase is SessionPhase.ESTABLISHED:
        params = (
            context.params
            if context is not None and context.params is not None
            else dict(dispatch.match.params)
        )
        cleanup_context = CleanupContext(
            resource=resource,
            dispatch=dispatch,
            hook_manager=hook_manager,
            session=session,
            params=dict(params),
            primary_error=primary_error,
        )
        cleanup_failures, interrupted = await run_shielded_cleanup(cleanup_context)
        raise_session_failures(primary_error, cleanup_failures, interrupted)

    return True


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
        except falcon.WebSocketDisconnected as exc:
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


async def _prepare_connection_context(
    hook_manager: HookManager,
    resource: WebSocketResource,
    dispatch: SessionDispatch,
) -> HookContext:
    """Return the hook context with the handler parameters resolved."""
    params_obj: dict[str, object] = dict(dispatch.match.params)
    context = await hook_manager.notify_before_connect(
        resource,
        req=dispatch.req,
        ws=dispatch.ws,
        params=params_obj,
    )
    # ``before_connect`` hooks may rewrite the parameters; fall back to the
    # route captures when they leave them untouched.
    if context.params is None:
        context.params = params_obj
    return context


async def _execute_resource_handler(
    resource: WebSocketResource,
    dispatch: SessionDispatch,
    context: HookContext,
    hook_manager: HookManager,
) -> bool:
    """Invoke ``resource.on_connect`` handling hook error propagation."""
    params = context.params if context.params is not None else {}
    try:
        return await resource.on_connect(dispatch.req, dispatch.ws, **params)
    except Exception as exc:
        context.error = exc
        context.result = False
        try:
            await hook_manager.notify_after_connect(context)
        except BaseException as after_error:
            if _is_websocket_accepted(dispatch.ws):
                raise_session_failures(exc, (after_error,), ())
            raise
        raise


async def _finalize_connection(
    ws: WebSocketLike,
    *,
    should_accept: bool,
    context: HookContext,
    hook_manager: HookManager,
) -> None:
    """Complete the connection lifecycle and honour ``should_accept``."""
    context.result = should_accept
    await hook_manager.notify_after_connect(context)
    session = hook_manager.session
    has_approval = should_accept or _is_websocket_accepted(ws)
    if session is not None and has_approval:
        session.establish()
    if not should_accept:
        await ws.close()
        return
    if not _is_websocket_accepted(ws):
        await ws.accept()


def _is_websocket_accepted(ws: WebSocketLike) -> bool:
    """Probe Falcon's public acceptance state without changing the protocol."""
    try:
        unaccepted = typ.cast("typ.Any", ws).unaccepted
    except AttributeError:
        return False
    return unaccepted is False


def _effective_disconnect_code(error: BaseException) -> int:
    """Return the effective close code, using abnormal closure when absent."""
    close_code = getattr(error, "code", None)
    return close_code if isinstance(close_code, int) else ABNORMAL_CLOSURE


def _record_session_ending(
    session: SessionState, error: BaseException
) -> BaseException | None:
    """Distinguish a transport ending from an unexpected session failure."""
    if isinstance(error, falcon.WebSocketDisconnected):
        session.close_code = _effective_disconnect_code(error)
        return None
    session.primary_error = error
    return error
