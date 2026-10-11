"""WebSocket routing utilities.

This module implements :class:`WebSocketRouter`, a Falcon resource that
dispatches incoming WebSocket connections based on path templates. Helper
functions handle template compilation and path normalization to support
both trailing and non-trailing slashes. The router can be mounted within a
Falcon app and used to generate URLs for registered routes.
"""

from __future__ import annotations

import collections.abc as cabc
import contextlib
import dataclasses as dc
import functools
import inspect
import re
import threading
import typing as typ

import falcon

from ._session import (
    ABNORMAL_CLOSURE,
    INTERNAL_ERROR,
    SessionPhase,
    SessionState,
)
from ._session_cleanup import (
    CleanupContext,
    raise_session_failures,
    run_shielded_cleanup,
)
from ._session_socket import receive_frame, track_session_socket
from ._uri_template import _compile_prefix_template, compile_uri_template
from .hooks import HookCollection, HookContext, HookManager
from .protocols import WebSocketLike

# Imported at runtime, not under TYPE_CHECKING: ``ResourceFactory`` below is a
# PEP 695 alias whose value is evaluated lazily on access. Exporting an alias
# that raises NameError when introspected would be a trap for runtime
# consumers, so the name it references has to resolve at runtime too.
from .resource import WebSocketResource

__all__ = ["ResourceFactory", "SimulatorFactory", "WebSocketRouter"]


type ResourceFactory = cabc.Callable[
    [cabc.Callable[..., WebSocketResource]],
    WebSocketResource,
]

type SimulatorFactory = cabc.Callable[
    [falcon.Request, WebSocketLike],
    WebSocketLike | cabc.Awaitable[WebSocketLike],
]


class _RequestLike(typ.Protocol):
    """Request surface consumed by the router before resource dispatch."""

    @property
    def path(self) -> str:
        """The request path."""


def _request_path_template(req: _RequestLike) -> str:
    """Return the mounted path template, defaulting like Falcon does."""
    template = getattr(req, "path_template", None)
    if template is None:
        template = getattr(req, "uri_template", "")

    # Falcon does not treat a `{name:path}` route as a mount prefix. The
    # descendant route therefore has to be reduced to the mounted identity.
    match = re.fullmatch(r"(?P<prefix>.*)/\{[A-Za-z_]\w*:path\}", template)
    if match is not None:
        return match.group("prefix") or "/"
    return template


def _normalize_path(path: str) -> str:
    """Ensure the path has a leading slash."""
    if not path.startswith("/"):
        path = f"/{path}"
    return path


def _canonical_path(path: str) -> str:
    """Return the normalized path without a trailing slash."""
    path = _normalize_path(path)
    if path != "/":
        path = path.rstrip("/")
    return path


@dc.dataclass(slots=True)
class _RouteMatch:
    """Captured route parameters plus the path segment still to be consumed."""

    params: dict[str, str]
    remaining: str


@dc.dataclass(slots=True)
class _Dispatch:
    """Mutable per-request state threaded through the routing pipeline."""

    route: WebSocketRouter._CompiledRoute
    req: falcon.Request
    ws: WebSocketLike
    match: _RouteMatch
    session: SessionState | None = None


class WebSocketRouter:
    """Route WebSocket connections to resources.

    Routes are evaluated in the order they were added. If multiple patterns
    overlap, the first match wins. Register more specific paths before more
    general ones to control precedence. Paths are normalized so that a template
    ``"/foo"`` matches ``"/foo"`` and ``"/foo/"`` equally. If a trailing slash is
    included in the template, generated URLs will preserve it.
    """

    @dc.dataclass(slots=True)
    class _RawRoute:
        template: str
        canonical: str
        factory: cabc.Callable[..., WebSocketResource]

    @dc.dataclass(slots=True)
    class _CompiledRoute:
        prefix: re.Pattern[str]
        pattern: re.Pattern[str]
        factory: cabc.Callable[..., WebSocketResource]

    def __init__(
        self,
        *,
        name: str | None = None,
        resource_factory: ResourceFactory | None = None,
        simulator_factory: SimulatorFactory | None = None,
    ) -> None:
        self._raw: list[WebSocketRouter._RawRoute] = []
        self._routes: list[WebSocketRouter._CompiledRoute] = []
        self._mount_prefix: str = ""
        self._mount_lock = threading.Lock()
        self._names: dict[str, str] = {}
        self._registration = _RouteRegistrationService(
            lock=self._mount_lock,
            raw_routes=self._raw,
            names=self._names,
        )
        self.global_hooks = HookCollection()
        self.name = name
        self._resource_factory: ResourceFactory
        self._resource_factory = resource_factory or (lambda factory: factory())
        self._simulator_factory = simulator_factory

    @staticmethod
    def _compile_route(
        canonical: str,
        factory: cabc.Callable[..., WebSocketResource],
        mount_prefix: str,
        existing_routes: cabc.Iterable[WebSocketRouter._CompiledRoute],
    ) -> WebSocketRouter._CompiledRoute:
        """Compile one route without mutating router registration state.

        Returns
        -------
        WebSocketRouter._CompiledRoute
            The compiled full and prefix patterns for the route.

        Raises
        ------
        ValueError
            If an identical pattern is already registered.
        """
        base = mount_prefix.rstrip("/")
        full = f"{base}{canonical}"
        pattern = compile_uri_template(full)
        prefix = _compile_prefix_template(full)
        for existing in existing_routes:
            if existing.pattern.pattern == pattern.pattern:
                msg = f"route path {full!r} already registered"
                raise ValueError(msg)

        return WebSocketRouter._CompiledRoute(prefix, pattern, factory)

    def mount(self, prefix: str) -> None:
        """Compile stored routes with the given mount ``prefix``."""
        if prefix and not prefix.startswith("/"):
            msg = "prefix must start with '/'"
            raise ValueError(msg)

        canonical = prefix.rstrip("/") or "/"

        with self._mount_lock:
            if self._mount_prefix:
                msg = f"router already mounted at '{self._mount_prefix}'"
                raise RuntimeError(msg)

            compiled_routes: list[WebSocketRouter._CompiledRoute] = []
            for raw in self._raw:
                compiled_routes.append(
                    self._compile_route(
                        raw.canonical,
                        raw.factory,
                        canonical,
                        self._routes + compiled_routes,
                    )
                )

            self._mount_prefix = canonical
            self._routes.extend(compiled_routes)

    def add_route(
        self,
        path: str,
        resource: type[WebSocketResource] | cabc.Callable[..., WebSocketResource],
        *init_args: object,
        name: str | None = None,
        **init_kwargs: object,
    ) -> None:
        """Register a WebSocketResource to handle ``path``.

        Any ``init_args`` or ``init_kwargs`` are stored and applied when a
        per-connection resource instance is created, so a single resource class
        can be configured differently across routes. ``name`` is reserved for
        the route name consumed by :meth:`url_for` and is therefore not
        forwarded to the resource.

        A resource whose own initializer takes a parameter called ``name``
        cannot receive it through ``init_kwargs``, because the keyword binds to
        the route name instead. Pass a factory for those, since ``resource``
        accepts any callable returning a :class:`WebSocketResource`::

            router.add_route(
                "/p", functools.partial(MyResource, name="alpha"), name="route"
            )
        """
        self._validate_resource_type(resource)
        path, canonical = self._registration.normalize_path(path)

        # Compile once to validate the template. The prefix is applied lazily
        # upon the first request since it may not yet be known at this point.
        compile_uri_template(canonical)

        factory = functools.partial(resource, *init_args, **init_kwargs)

        with self._mount_lock:
            self._registration.check_conflicts(canonical, name, path=path)
            compiled_route: WebSocketRouter._CompiledRoute | None = None
            if self._mount_prefix:
                compiled_route = self._compile_route(
                    canonical, factory, self._mount_prefix, self._routes
                )
            self._raw.append(WebSocketRouter._RawRoute(path, canonical, factory))
            if name:
                self._names[name] = path
            if compiled_route is not None:
                self._routes.append(compiled_route)

    @staticmethod
    def _validate_resource_type(
        resource: type[WebSocketResource] | cabc.Callable[..., WebSocketResource],
    ) -> None:
        """Ensure ``resource`` can be called to create a handler.

        Raises
        ------
        TypeError
            If ``resource`` is not callable.
        """
        if not callable(resource):
            msg = "resource must be callable"
            raise TypeError(msg)

    def url_for(self, name: str, **params: object) -> str:
        """Return the URL path associated with ``name`` formatted with ``params``."""
        try:
            template = self._names[name]
        except KeyError as exc:
            msg = f"no route registered with name {name!r}"
            raise KeyError(msg) from exc

        return _normalize_path(template.format(**params))

    async def on_websocket(
        self, req: _RequestLike, ws: WebSocketLike, **_params: object
    ) -> None:
        """Own the routed session through disconnect and completed cleanup.

        ``req.path_template`` is assumed to be a prefix of ``req.path``. If the
        assumption fails, :class:`falcon.HTTPNotFound` is raised to signal that
        the requested path does not map to this router's mount point. Falcon
        may pass route parameters for a mounted ``{name:path}`` descendant; the
        router matches against ``req.path`` and does not need those values.

        Raises
        ------
        falcon.HTTPNotFound
            If ``req.path_template`` does not match this router's mount point,
            or if no registered route matches ``req.path``.
        """
        # Handle missing or empty path_template by defaulting to root "/"
        prefix = _request_path_template(req).rstrip("/") or "/"
        if prefix != self._mount_prefix:
            msg = (
                f"path_template '{prefix}' does not match router mount "
                f"'{self._mount_prefix}'"
            )
            raise falcon.HTTPNotFound(description=msg)

        # Routes are tested in the order they were added. Register more
        # specific paths before general ones to control precedence.
        for route in self._routes:
            if await self._try_route(route, req, ws):
                return

        raise falcon.HTTPNotFound

    async def _try_route(
        self, route: _CompiledRoute, req: _RequestLike, ws: WebSocketLike
    ) -> bool:
        """Attempt to handle ``req`` using ``route``.

        The routing sequence is as follows:

        1. :meth:`_validate_and_normalize_path` ensures ``req.path`` matches the
           route's prefix and returns any captured parameters plus the remaining
           path.
        2. A base resource instance is created from the route's factory.
        3. :meth:`_resolve_resource_and_path` walks any nested subroutes on the
           base resource using the remaining path, merging parameters at each
           level.
        4. If a final resource and empty path are resolved, control is passed to
           :meth:`_handle_websocket_connection` which accepts or closes the
           connection via the resource's ``on_connect`` method.

        Returns
        -------
        bool
            ``True`` if the request was handled by this route, ``False``
            otherwise.
        """
        match = self._validate_and_normalize_path(route, req)
        if match is None:
            return False
        # cast: downstream hooks and ``on_connect`` are typed against
        # ``falcon.Request``; the router accepts any ``_RequestLike`` so tests
        # can supply structural stand-ins.
        return await self._execute_route_with_error_handling(
            _Dispatch(
                route=route, req=typ.cast("falcon.Request", req), ws=ws, match=match
            )
        )

    async def _execute_route_with_error_handling(self, dispatch: _Dispatch) -> bool:
        """Run the routing pipeline and close the socket on unexpected errors."""
        ws_for_cleanup: WebSocketLike = dispatch.ws
        try:
            dispatch.ws = await self._prepare_websocket(dispatch.req, dispatch.ws)
            ws_for_cleanup = dispatch.ws
            return await self._process_route_resolution(dispatch)
        except Exception as exc:
            session_is_established = (
                dispatch.session is not None
                and dispatch.session.phase is not SessionPhase.CONNECTING
            )
            if not session_is_established and not getattr(
                exc, "_pachinko_factory_closed", False
            ):
                with contextlib.suppress(Exception):
                    await ws_for_cleanup.close()
            raise

    async def _prepare_websocket(
        self, req: falcon.Request, ws: WebSocketLike
    ) -> WebSocketLike:
        """Return the WebSocket to use for ``req``.

        Returns
        -------
        WebSocketLike
            The original socket, or the object produced by a configured
            ``simulator_factory``.

        Raises
        ------
        TypeError
            If a configured ``simulator_factory`` returns an object that does
            not expose the :class:`WebSocketLike` surface.
        """
        # When :attr:`_simulator_factory` is configured the factory may wrap or
        # replace ``ws`` with an injectable simulator. The returned object is
        # validated to expose the :class:`WebSocketLike` surface used by the
        # router.
        if self._simulator_factory is None:
            return ws

        candidate = self._simulator_factory(req, ws)
        if inspect.isawaitable(candidate):
            candidate = await candidate

        for attr in ("accept", "close", "send_media", "receive_media"):
            if not self._is_valid_websocket_method(candidate, attr):
                msg = (
                    "simulator_factory must return a WebSocketLike object; "
                    f"missing attribute {attr!r}"
                )
                raise TypeError(msg)

        # cast: the duck-type checks above establish the ``WebSocketLike``
        # surface, which the checker cannot infer from getattr-based checks.
        return typ.cast("WebSocketLike", candidate)

    @staticmethod
    def _is_valid_websocket_method(obj: object, attr_name: str) -> bool:
        """Return ``True`` when ``obj`` exposes coroutine ``attr_name``."""
        func = getattr(obj, attr_name, None)
        if func is None or not callable(func):
            return False
        return inspect.iscoroutinefunction(func)

    async def _process_route_resolution(self, dispatch: _Dispatch) -> bool:
        """Resolve the final resource and dispatch the connection."""
        base_resource = await self._instantiate_resource(
            dispatch.route.factory, dispatch.ws
        )
        chain = [base_resource]
        resource = await self._resolve_resource_and_path(
            base_resource, dispatch.match, chain, dispatch.ws
        )
        if resource is None:
            return False
        if not self._validate_final_resource(
            resource, base_resource, dispatch.route, dispatch.req
        ):
            return False
        manager = self._setup_hook_management(chain)
        return await self._handle_websocket_connection(
            resource, dispatch, hook_manager=manager
        )

    @staticmethod
    def _validate_final_resource(
        resource: WebSocketResource,
        base_resource: WebSocketResource,
        route: _CompiledRoute,
        req: falcon.Request,
    ) -> bool:
        """Return ``True`` if ``resource`` is usable for ``req``."""
        return not (resource is base_resource and not route.pattern.fullmatch(req.path))

    def _setup_hook_management(self, chain: list[WebSocketResource]) -> HookManager:
        """Attach a :class:`HookManager` to every resource in ``chain``."""
        manager = HookManager(global_hooks=self.global_hooks, resources=chain)
        for item in chain:
            item.bind_hook_manager(manager)
        return manager

    @staticmethod
    def _normalize_path_remaining(remaining: str, match: re.Match[str]) -> str | None:
        """Normalize ``remaining`` or return ``None`` if invalid."""
        if not remaining or remaining.startswith("/"):
            return remaining
        return f"/{remaining}" if match.group(0).endswith("/") else None

    async def _try_subroute_match(
        self, resource: WebSocketResource, path: str, ws: WebSocketLike
    ) -> tuple[WebSocketResource, str, dict[str, str]] | None:
        """Return matched subroute components or ``None``."""
        for pattern, factory in getattr(resource, "_subroutes", []):
            if match := pattern.match(path):
                remaining = path[match.end() :]
                if (
                    remaining := self._normalize_path_remaining(remaining, match)
                ) is None:
                    return None
                context = resource.get_child_context()
                child_kwargs = {k: v for k, v in context.items() if k != "state"}
                child_factory = functools.partial(factory, **child_kwargs)
                new_resource = await self._instantiate_resource(child_factory, ws)
                state_mapping = context.get("state", resource.state)
                # cast: the context value is typed ``object``; the ``state``
                # setter validates the mapping surface at runtime.
                new_resource.state = typ.cast(
                    "cabc.MutableMapping[str, typ.Any]",
                    state_mapping,
                )
                params = match.groupdict()
                return new_resource, remaining, params
        return None

    async def _resolve_subroutes(
        self,
        resource: WebSocketResource,
        match: _RouteMatch,
        chain: list[WebSocketResource],
        ws: WebSocketLike,
    ) -> WebSocketResource:
        """Traverse ``resource`` subroutes, consuming ``match`` in place."""
        while match.remaining not in {"", "/"}:
            result = await self._try_subroute_match(resource, match.remaining, ws)
            if result is None:
                break
            resource, match.remaining, new_params = result
            match.params |= new_params
            chain.append(resource)

        return resource

    def _validate_and_normalize_path(
        self, route: _CompiledRoute, req: _RequestLike
    ) -> _RouteMatch | None:
        """Return the path match for ``req`` or ``None`` if invalid."""
        if not (prefix_match := route.prefix.match(req.path)):
            return None
        remaining = req.path[prefix_match.end() :]
        if remaining and not remaining.startswith("/"):
            normalized = self._normalize_path_remaining(remaining, prefix_match)
            if normalized is None:
                return None
            remaining = normalized
        return _RouteMatch(params=prefix_match.groupdict(), remaining=remaining)

    async def _resolve_resource_and_path(
        self,
        resource: WebSocketResource,
        match: _RouteMatch,
        chain: list[WebSocketResource],
        ws: WebSocketLike,
    ) -> WebSocketResource | None:
        """Return the resolved resource, or ``None`` if the path is unconsumed."""
        resolved = await self._resolve_subroutes(resource, match, chain, ws)
        return resolved if match.remaining in {"", "/"} else None

    async def _instantiate_resource(
        self,
        route_factory: cabc.Callable[..., WebSocketResource],
        ws: WebSocketLike,
    ) -> WebSocketResource:
        """Instantiate a resource using ``route_factory`` with error handling."""
        try:
            return self._resource_factory(route_factory)
        except Exception as exc:  # pragma: no cover - exercise via tests
            await ws.close()
            # cast: the checker cannot see the dynamic marker attribute used
            # to signal that the socket was already closed for this failure.
            typ.cast("typ.Any", exc)._pachinko_factory_closed = True
            raise

    async def _handle_websocket_connection(
        self,
        resource: WebSocketResource,
        dispatch: _Dispatch,
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
            context = await self._prepare_connection_context(
                hook_manager, resource, dispatch
            )
            should_accept = await self._execute_resource_handler(
                resource, dispatch, context, hook_manager
            )
            if should_accept or _is_websocket_accepted(dispatch.ws):
                session.establish()
            await self._finalize_connection(
                dispatch.ws,
                should_accept=should_accept,
                context=context,
                hook_manager=hook_manager,
            )
            if session.can_receive:
                await self._receive_session(resource, dispatch, session, hook_manager)
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

    @staticmethod
    async def _receive_session(
        resource: WebSocketResource,
        dispatch: _Dispatch,
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

    @staticmethod
    async def _prepare_connection_context(
        hook_manager: HookManager,
        resource: WebSocketResource,
        dispatch: _Dispatch,
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

    @staticmethod
    async def _execute_resource_handler(
        resource: WebSocketResource,
        dispatch: _Dispatch,
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

    @staticmethod
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


class _RouteRegistrationService:
    """Manage route normalization and conflict detection for a router."""

    def __init__(
        self,
        *,
        lock: threading.Lock,
        raw_routes: list[WebSocketRouter._RawRoute],
        names: dict[str, str],
    ) -> None:
        self._lock = lock
        self._raw_routes = raw_routes
        self._names = names

    @staticmethod
    def normalize_path(path: str) -> tuple[str, str]:
        """Return normalized and canonical variants of ``path``."""
        normalized = _normalize_path(path)
        canonical = _canonical_path(normalized)
        return normalized, canonical

    def check_conflicts(
        self, canonical: str, name: str | None, *, path: str | None = None
    ) -> None:
        """Raise if ``canonical`` or ``name`` already exists under ``lock``.

        Raises
        ------
        RuntimeError
            If the mount lock is not held by the caller.
        ValueError
            If the canonical path or the route name is already registered.
        """
        if not self._lock.locked():
            msg = (
                "_RouteRegistrationService.check_conflicts requires "
                "_mount_lock to be held"
            )
            raise RuntimeError(msg)

        display_path = path if path is not None else canonical
        if any(route.canonical == canonical for route in self._raw_routes):
            msg = f"route path {display_path!r} already registered"
            raise ValueError(msg)
        if name and name in self._names:
            msg = f"route name {name!r} already registered"
            raise ValueError(msg)
