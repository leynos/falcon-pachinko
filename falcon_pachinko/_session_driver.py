"""Drive the WebSocket lifecycle after the router selects its resource.

The router awaits ``run_session`` to preserve setup ordering, receive dispatch,
and exactly-once disconnect cleanup. Session state is shared with the bound
hook manager, while shielded cleanup preserves failure and cancellation policy.
"""

from __future__ import annotations

import abc
import asyncio
import dataclasses as dc
import logging
import typing as typ

import falcon

from ._disconnect import _PeerDisconnectedError
from ._session import (
    ABNORMAL_CLOSURE,
    GOING_AWAY,
    INTERNAL_ERROR,
    effective_close_code,
)
from ._session_cleanup import (
    CleanupContext,
    raise_session_failures,
    run_shielded_cleanup,
)
from ._session_socket import receive_frame
from .diagnostics import _type_name

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from ._session import SessionState
    from .hooks import HookManager
    from .lifecycle import LifecycleGuard
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
class SessionDriverContext:
    """Retain the values shared by session execution and finalization."""

    resource: WebSocketResource
    resources: tuple[WebSocketResource, ...]
    websocket: LifecycleGuard
    dispatch: SessionDispatch
    session_state: SessionState
    hook_manager: HookManager
    params: cabc.Mapping[str, object]
    primary_error: BaseException | None = None
    finalization_error: BaseException | None = None


async def run_session(
    context: SessionDriverContext,
    finalize: cabc.Callable[
        [BaseException | None, BaseException | None], cabc.Awaitable[None]
    ],
) -> None:
    """Run post-accept setup, frame dispatch, and exactly-once cleanup."""
    primary_error: BaseException | None = None
    finalization_error: BaseException | None = None

    try:
        await _run_session_body(context)
    except asyncio.CancelledError as exc:
        primary_error, finalization_error = await _handle_session_failure(context, exc)
    except Exception as exc:  # ruff: ignore[blind-except] - hooks are extensible
        primary_error, finalization_error = await _handle_session_failure(context, exc)
    finally:
        finalization_error = await _close_after_session_run(context, finalization_error)
        await finalize(primary_error, finalization_error)


async def _run_session_body(context: SessionDriverContext) -> None:
    """Run resource setup and the receive loop before finalization."""
    await context.resource.on_session_start(context.websocket)
    if context.hook_manager.can_receive(context.websocket):
        await _receive_session(
            context.resource,
            context.dispatch,
            context.session_state,
            context.hook_manager,
        )


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
        context.session_state.close_code = ABNORMAL_CLOSURE
        return None

    close_code = INTERNAL_ERROR
    if context.session_state.close_code is None:
        context.session_state.close_code = close_code
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
    primary_error = _record_session_ending(context.session_state, error)
    if primary_error is not None and not isinstance(
        primary_error, asyncio.CancelledError
    ):
        logger.error("WebSocket session failed: %s", _type_name(primary_error))
    finalization_error = await _close_after_session_error(context, primary_error)
    return primary_error, finalization_error


async def finalize_session(context: SessionDriverContext) -> None:
    """Close if needed, run disconnect hooks, then apply error precedence."""
    session = context.session_state
    recorded_code = getattr(context.websocket, "close_code", None)
    session.close_code = effective_close_code(
        recorded_code, session, context.primary_error
    )

    cleanup_context = CleanupContext(
        resource=context.resource,
        dispatch=context.dispatch,
        hook_manager=context.hook_manager,
        session=session,
        params=dict(context.params),
        primary_error=context.primary_error,
    )
    cleanup_failures, interrupted = await run_shielded_cleanup(cleanup_context)
    failures = (
        (context.finalization_error,) if context.finalization_error is not None else ()
    ) + cleanup_failures
    raise_session_failures(context.primary_error, failures, interrupted)


async def _receive_session(
    resource: WebSocketResource,
    dispatch: SessionDispatch,
    session: SessionState,
    hook_manager: HookManager,
) -> None:
    """Receive and route frames until Falcon reports a disconnect."""
    while hook_manager.can_receive(dispatch.ws):
        try:
            raw = await receive_frame(dispatch.ws)
        except (falcon.WebSocketDisconnected, _PeerDisconnectedError) as exc:
            session.peer_disconnected = True
            session.close_code = _effective_disconnect_code(exc)
            return

        # A close can be initiated by another task while receive_media is
        # suspended. Do not dispatch the late frame into receive hooks.
        if not hook_manager.can_receive(dispatch.ws):
            return
        await resource.dispatch(dispatch.ws, raw)


def _effective_disconnect_code(error: BaseException) -> int:
    """Return the effective close code, using abnormal closure when absent."""
    close_code = getattr(error, "code", None)
    return close_code if isinstance(close_code, int) else ABNORMAL_CLOSURE


def _record_session_ending(
    session: SessionState, error: BaseException
) -> BaseException | None:
    """Distinguish a transport ending from an unexpected session failure."""
    if isinstance(error, (falcon.WebSocketDisconnected, _PeerDisconnectedError)):
        session.peer_disconnected = True
        if session.close_code is None:
            session.close_code = _effective_disconnect_code(error)
        return None
    session.primary_error = error
    return error
