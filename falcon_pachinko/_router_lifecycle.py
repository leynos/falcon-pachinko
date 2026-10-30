"""Connection negotiation and cleanup for routed WebSocket sessions."""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses as dc
import inspect
import typing as typ

import falcon

from ._router_resources import _ResourceResolver
from ._session import SessionPhase, SessionState
from ._session_driver import SessionDriverContext
from ._session_socket import track_session_socket
from .lifecycle import AcceptOptions, ConnectionPhase, LifecycleGuard, WebSocketSession

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    import re

    from ._route_support import _RouteMatch
    from .hooks import HookContext, HookManager
    from .observability import LifecycleObserver
    from .protocols import WebSocketLike
    from .resource import WebSocketResource


class _RouteSpec(typ.Protocol):
    """Route fields needed while negotiating a connection."""

    pattern: re.Pattern[str]
    factory: cabc.Callable[..., WebSocketResource]


@dc.dataclass(slots=True)
class _Dispatch:
    """Mutable per-request state threaded through the routing pipeline."""

    route: _RouteSpec
    req: falcon.Request
    ws: WebSocketLike
    match: _RouteMatch
    session: SessionState | None = None
    matched: bool = False
    observer: LifecycleObserver | None = None


@dc.dataclass(slots=True)
class _ConnectionPreparation:
    """Reuse the guarded transport while the router tries route candidates."""

    session: SessionState = dc.field(default_factory=SessionState)
    websocket: LifecycleGuard | None = None
    observer: LifecycleObserver | None = None


class _RouterServices(typ.Protocol):
    """Resource operations used by the connection negotiation state machine."""

    _resource_factory: cabc.Callable[
        [cabc.Callable[..., WebSocketResource]], WebSocketResource
    ]

    _simulator_factory: (
        cabc.Callable[
            [falcon.Request, WebSocketLike],
            WebSocketLike | cabc.Awaitable[WebSocketLike],
        ]
        | None
    )

    async def _resolve_resource_and_path(
        self,
        resource: WebSocketResource,
        match: _RouteMatch,
        chain: list[WebSocketResource],
        ws: WebSocketLike,
    ) -> WebSocketResource | None:
        """Resolve nested resources and consume the remaining route path."""

    def _setup_hook_management(
        self, chain: list[WebSocketResource], session: SessionState
    ) -> HookManager:
        """Bind hooks to all resources in the selected chain."""


@dc.dataclass(slots=True)
class _RouteLifecycle:
    """Share accepted-connection state across negotiation and cleanup."""

    resource: WebSocketResource
    resources: list[WebSocketResource]
    dispatch: _Dispatch
    session: SessionState
    hook_manager: HookManager
    params: dict[str, object]
    observer: LifecycleObserver | None

    @property
    def websocket(self) -> LifecycleGuard:
        """The guarded transport selected for this route."""
        return typ.cast("LifecycleGuard", self.dispatch.ws)


async def execute_route_with_error_handling(
    router: _RouterServices,
    dispatch: _Dispatch,
    preparation: _ConnectionPreparation,
) -> WebSocketSession | None:
    """Adapt and guard the socket, resolving its resource with safe cleanup."""
    ws_for_cleanup: WebSocketLike = dispatch.ws
    try:
        session = _session_state(dispatch)
        dispatch.observer = preparation.observer
        if preparation.websocket is None:
            dispatch.ws = await _prepare_websocket(
                router, dispatch, session, preparation
            )
            preparation.websocket = dispatch.ws
        else:
            dispatch.ws = preparation.websocket
        ws_for_cleanup = dispatch.ws
        return await _process_route_resolution(router, dispatch)
    except (Exception, asyncio.CancelledError):
        if isinstance(ws_for_cleanup, LifecycleGuard):
            if ws_for_cleanup.phase is ConnectionPhase.PRE_ACCEPT:
                with contextlib.suppress(BaseException):
                    await ws_for_cleanup.close()
        else:
            with contextlib.suppress(BaseException):
                await ws_for_cleanup.close()
        raise


async def _prepare_websocket(
    router: _RouterServices,
    dispatch: _Dispatch,
    session: SessionState,
    preparation: _ConnectionPreparation,
) -> LifecycleGuard:
    """Adapt and guard the socket before constructing route resources."""
    candidate = await _simulator_candidate(router, dispatch.req, dispatch.ws)
    adapted = track_session_socket(typ.cast("WebSocketLike", candidate), session)
    return LifecycleGuard(adapted, session, observer=preparation.observer)


async def _simulator_candidate(
    router: _RouterServices,
    req: falcon.Request,
    ws: WebSocketLike,
) -> object:
    """Return the configured transport and await asynchronous factories."""
    factory = router._simulator_factory
    if factory is None:
        return ws
    candidate = factory(req, ws)
    if inspect.isawaitable(candidate):
        candidate = await candidate
    _validate_simulator_candidate(candidate)
    return candidate


def _validate_simulator_candidate(candidate: object) -> None:
    """Require the async methods used by the guarded socket contract."""
    for attr in ("accept", "close", "send_media", "receive_media"):
        if not _is_valid_websocket_method(candidate, attr):
            msg = (
                "simulator_factory must return a WebSocketLike object; "
                f"missing attribute {attr!r}"
            )
            raise TypeError(msg)


def _is_valid_websocket_method(obj: object, attr_name: str) -> bool:
    """Return ``True`` when ``obj`` exposes coroutine ``attr_name``."""
    func = getattr(obj, attr_name, None)
    return func is not None and callable(func) and inspect.iscoroutinefunction(func)


async def _process_route_resolution(
    router: _RouterServices, dispatch: _Dispatch
) -> WebSocketSession | None:
    """Resolve the final resource and negotiate the connection."""
    base_resource = await _ResourceResolver(router._resource_factory).instantiate(
        dispatch.route.factory, dispatch.ws
    )
    chain = [base_resource]
    resource = await router._resolve_resource_and_path(
        base_resource, dispatch.match, chain, dispatch.ws
    )
    if resource is None:
        return None
    if not _validate_final_resource(
        resource, base_resource, dispatch.route, dispatch.req
    ):
        return None
    dispatch.matched = True
    session = _session_state(dispatch)
    manager = router._setup_hook_management(chain, session)
    lifecycle = _RouteLifecycle(
        resource=resource,
        resources=chain,
        dispatch=dispatch,
        session=session,
        hook_manager=manager,
        params=dict(dispatch.match.params),
        observer=dispatch.observer,
    )
    return await _negotiate_connection(lifecycle)


async def _negotiate_connection(
    lifecycle: _RouteLifecycle,
) -> WebSocketSession | None:
    """Run pre-accept hooks, then create a handle for accepted sessions."""
    dispatch = lifecycle.dispatch
    hook_context = await lifecycle.hook_manager.notify_before_connect(
        lifecycle.resource,
        req=dispatch.req,
        ws=lifecycle.websocket,
        params=lifecycle.params,
    )
    if hook_context.params is None:
        hook_context.params = lifecycle.params
    lifecycle.params = dict(hook_context.params)

    try:
        decision = await lifecycle.resource.on_connect(
            dispatch.req, lifecycle.websocket, **lifecycle.params
        )
        normalized_decision = normalize_accept_decision(decision)
    except asyncio.CancelledError as exc:
        return await _handle_on_connect_failure(exc, hook_context, lifecycle)
    except Exception as exc:  # ruff: ignore[blind-except] - resource callback
        return await _handle_on_connect_failure(exc, hook_context, lifecycle)
    return await _complete_negotiation(normalized_decision, hook_context, lifecycle)


async def _handle_on_connect_failure(
    error: BaseException,
    hook_context: HookContext,
    lifecycle: _RouteLifecycle,
) -> WebSocketSession | None:
    """Run the remaining setup hooks and preserve the original failure."""
    hook_context.error = error
    hook_context.result = False
    after_connect_error = await _after_connect_error(hook_context, lifecycle)

    if isinstance(error, falcon.WebSocketDisconnected):
        await _cleanup_negotiation_failure(None, lifecycle, after_connect_error)
        if after_connect_error is None:
            return None
        raise after_connect_error from error

    primary_error, cleanup_error = connection_setup_errors(error, after_connect_error)
    await _cleanup_negotiation_failure(primary_error, lifecycle, cleanup_error)
    if primary_error is error:
        raise error
    raise primary_error from error


async def _after_connect_error(
    hook_context: HookContext, lifecycle: _RouteLifecycle
) -> BaseException | None:
    """Run after-connect hooks and return a failure instead of replacing one."""
    try:
        await lifecycle.hook_manager.notify_after_connect(hook_context)
    except asyncio.CancelledError as error:
        return error
    except Exception as error:  # ruff: ignore[blind-except] - hook errors
        return error
    return None


async def _complete_negotiation(
    decision: tuple[bool, AcceptOptions | None],
    hook_context: HookContext,
    lifecycle: _RouteLifecycle,
) -> WebSocketSession | None:
    """Apply the decision after hooks, then accept or reject once."""
    try:
        return await _apply_accept_decision(decision, hook_context, lifecycle)
    except (Exception, asyncio.CancelledError) as error:
        _record_negotiation_error(error, hook_context)
        await _cleanup_negotiation_failure(error, lifecycle)
        raise


async def _apply_accept_decision(
    decision: tuple[bool, AcceptOptions | None],
    hook_context: HookContext,
    lifecycle: _RouteLifecycle,
) -> WebSocketSession | None:
    """Run completion hooks, then accept or reject the negotiated session."""
    accepted, options = decision
    hook_context.result = accepted
    await lifecycle.hook_manager.notify_after_connect(hook_context)
    if not accepted or lifecycle.websocket.phase is ConnectionPhase.CLOSED:
        await _reject_connection(lifecycle)
        return None
    if lifecycle.websocket.phase is ConnectionPhase.PRE_ACCEPT:
        await lifecycle.websocket._accept(options)
    return _new_session(lifecycle)


async def _reject_connection(lifecycle: _RouteLifecycle) -> None:
    """Finalize legacy accepted sessions or reject before acceptance."""
    if lifecycle.session.phase is not SessionPhase.CONNECTING:
        await _new_session(lifecycle).aclose()
    else:
        await lifecycle.websocket.close()


def _record_negotiation_error(error: BaseException, hook_context: HookContext) -> None:
    """Expose ordinary setup errors to cleanup hooks without replacing them."""
    if isinstance(error, asyncio.CancelledError):
        return
    hook_context.error = error
    hook_context.result = False


async def _cleanup_negotiation_failure(
    error: BaseException | None,
    lifecycle: _RouteLifecycle,
    finalization_error: BaseException | None = None,
) -> None:
    """Close failures before acceptance and finalize legacy accepted errors."""
    if lifecycle.session.phase is not SessionPhase.CONNECTING:
        await _finalize_accepted_failure(error, finalization_error, lifecycle)
        return
    if lifecycle.websocket.phase is ConnectionPhase.PRE_ACCEPT:
        await _close_pre_accept_failure(error, lifecycle)


async def _finalize_accepted_failure(
    error: BaseException | None,
    finalization_error: BaseException | None,
    lifecycle: _RouteLifecycle,
) -> None:
    """Run accepted-session cleanup and preserve its original setup failure."""
    try:
        await _new_session(lifecycle)._finalize(error, finalization_error)
    except BaseException:
        if error is None:
            raise


async def _close_pre_accept_failure(
    error: BaseException | None, lifecycle: _RouteLifecycle
) -> None:
    """Close a rejected handshake unless close itself is the primary error."""
    try:
        await lifecycle.websocket.close()
    except BaseException:
        if error is None:
            raise


def _new_session(lifecycle: _RouteLifecycle) -> WebSocketSession:
    """Build the retained handle for an accepted connection."""
    return WebSocketSession(
        SessionDriverContext(
            resource=lifecycle.resource,
            resources=tuple(lifecycle.resources),
            websocket=lifecycle.websocket,
            dispatch=lifecycle.dispatch,
            session_state=lifecycle.session,
            hook_manager=lifecycle.hook_manager,
            params=lifecycle.params,
            observer=lifecycle.observer,
        )
    )


def _session_state(dispatch: _Dispatch) -> SessionState:
    """Return the session state or expose a broken dispatch invariant."""
    if dispatch.session is None:
        msg = "dispatch session state is required"
        raise RuntimeError(msg)
    return dispatch.session


def _validate_final_resource(
    resource: WebSocketResource,
    base_resource: WebSocketResource,
    route: _RouteSpec,
    req: falcon.Request,
) -> bool:
    """Return ``True`` if ``resource`` is usable for ``req``."""
    return not (resource is base_resource and not route.pattern.fullmatch(req.path))


def normalize_accept_decision(
    decision: object,
) -> tuple[bool, AcceptOptions | None]:
    """Convert supported hook results to an acceptance decision."""
    if not decision:
        return False, None
    if decision is True:
        return True, None
    if isinstance(decision, AcceptOptions):
        return True, decision
    msg = "on_connect must return a falsey value, True, or AcceptOptions"
    raise TypeError(msg)


def connection_setup_errors(
    primary: BaseException,
    secondary: BaseException | None,
) -> tuple[BaseException, BaseException | None]:
    """Retain setup failures while giving cancellation precedence."""
    match secondary:
        case None:
            return primary, None
        case asyncio.CancelledError():
            return secondary, primary
        case Exception() as error if isinstance(primary, Exception):
            group = ExceptionGroup("session", [primary, error])
            return group, None
        case _:
            return primary, secondary
