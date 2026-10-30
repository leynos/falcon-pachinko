"""WebSocket routing utilities.

This module implements :class:`WebSocketRouter`, a Falcon resource that
dispatches incoming WebSocket connections based on path templates. Helper
functions handle template compilation and path normalization to support
both trailing and non-trailing slashes. The router can be mounted within a
Falcon app and used to generate URLs for registered routes.
"""

from __future__ import annotations

import asyncio
import collections.abc as cabc
import contextlib
import dataclasses as dc
import functools
import inspect
import threading
import time
import typing as typ

import falcon
import falcon.asgi

from ._route_support import (
    _CompiledRoute,
    _normalize_path,
    _normalize_path_remaining,
    _RawRoute,
    _request_path_template,
    _RequestLike,
    _RouteMatch,
    _RouteRegistrationService,
    _strip_terminal_greedy_path_field,
)
from ._router_attachment import attach_router
from ._router_resources import _ResourceResolver
from ._session_driver import _SessionOptions, run_session
from ._uri_template import _compile_prefix_template, compile_uri_template
from .hooks import HookCollection, HookManager
from .observability import (
    NOOP_SESSION_INSTRUMENTATION,
    SessionInstrumentation,
)
from .protocols import WebSocketLike

# Imported at runtime, not under TYPE_CHECKING: ``ResourceFactory`` below is a
# PEP 695 alias whose value is evaluated lazily on access. Exporting an alias
# that raises NameError when introspected would be a trap for runtime
# consumers, so the name it references has to resolve at runtime too.
from .resource import WebSocketResource

if typ.TYPE_CHECKING:
    from ._session import SessionState

__all__ = ["ResourceFactory", "SimulatorFactory", "WebSocketRouter"]


type ResourceFactory = cabc.Callable[
    [cabc.Callable[..., WebSocketResource]],
    WebSocketResource,
]

type SimulatorFactory = cabc.Callable[
    [falcon.Request, WebSocketLike],
    WebSocketLike | cabc.Awaitable[WebSocketLike],
]


def _guard_session_clock(
    clock: cabc.Callable[[], float],
) -> cabc.Callable[[], float]:
    """Keep diagnostics clock failures from bypassing session cleanup."""

    def read_clock() -> float:
        try:
            return clock()
        except (Exception, asyncio.CancelledError):  # ruff: ignore[blind-except] - isolate timing failures so cleanup always runs
            # Callback cancellation is telemetry failure, not task cancellation.
            return time.monotonic()

    return read_clock


@dc.dataclass(slots=True)
class _Dispatch:
    """Mutable per-request state threaded through the routing pipeline."""

    route: _CompiledRoute
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

    def __init__(
        self,
        *,
        name: str | None = None,
        resource_factory: ResourceFactory | None = None,
        simulator_factory: SimulatorFactory | None = None,
        receive_loop: bool = False,
    ) -> None:
        """Create a router and configure its accepted-session receive loop.

        Parameters
        ----------
        name : str or None, default=None
            Optional router name used by route registration.
        resource_factory : ResourceFactory or None, default=None
            Optional factory used to create routed resources.
        simulator_factory : SimulatorFactory or None, default=None
            Optional factory used by simulator routing.
        receive_loop : bool, default=False
            If true, receive and dispatch frames in order after ``on_connect``
            permits the session and attempt ``on_disconnect`` once, including
            acceptance setup failure. ``attach()`` and
            ``configure_raw_frame_media(app)`` share the User Guide's app-wide
            raw-media setup. If false, return after observed acceptance; the
            application owns receiving and cleanup. Approval without completed
            acceptance is cleaned up by the router before it returns.
        """
        self._raw: list[_RawRoute] = []
        self._routes: list[_CompiledRoute] = []
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
        self._receive_loop = receive_loop
        self._attached = False
        self._session_instrumentation: SessionInstrumentation = (
            NOOP_SESSION_INSTRUMENTATION
        )
        self._session_started = False
        self._session_clock: cabc.Callable[[], float] = time.monotonic

    def configure_session_instrumentation(
        self,
        instrumentation: SessionInstrumentation | None,
        *,
        monotonic_clock: cabc.Callable[[], float] | None = None,
    ) -> None:
        """Configure optional host-owned metrics and timing before first use.

        The router has a no-op implementation by default. The configured
        adapter must use synchronous, non-blocking callbacks and bounded
        exporter work; it is invoked on the session's event-loop thread.
        An optional monotonic clock supplies lifecycle observation and
        instrumentation durations. If it raises, the router uses
        ``time.monotonic`` so timing cannot bypass accepted-session cleanup.

        Parameters
        ----------
        instrumentation : SessionInstrumentation or None
            Host adapter, or ``None`` to restore the no-op implementation.
        monotonic_clock : collections.abc.Callable or None, default=None
            Monotonic clock used for lifecycle observations and instrumentation.
            ``None`` restores ``time.monotonic``.

        Raises
        ------
        RuntimeError
            If a routed session has already started on this router.
        """
        with self._mount_lock:
            if self._session_started:
                msg = "session instrumentation must be configured before first use"
                raise RuntimeError(msg)
            self._session_instrumentation = (
                NOOP_SESSION_INSTRUMENTATION
                if instrumentation is None
                else instrumentation
            )
            self._session_clock = _guard_session_clock(
                time.monotonic if monotonic_clock is None else monotonic_clock
            )

    @staticmethod
    def _compile_route(
        canonical: str,
        factory: cabc.Callable[..., WebSocketResource],
        mount_prefix: str,
        existing_routes: cabc.Iterable[_CompiledRoute],
    ) -> _CompiledRoute:
        """Compile one route without mutating router registration state.

        Returns
        -------
        _CompiledRoute
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

        return _CompiledRoute(prefix, pattern, factory)

    def mount(self, prefix: str) -> None:
        """Compile stored routes with the given mount ``prefix``."""
        canonical = self._canonical_mount_prefix(prefix)

        with self._mount_lock:
            self._mount_locked(canonical)

    @staticmethod
    def _canonical_mount_prefix(prefix: str) -> str:
        """Validate and normalize a mount prefix."""
        if prefix and not prefix.startswith("/"):
            msg = "prefix must start with '/'"
            raise ValueError(msg)

        return prefix.rstrip("/") or "/"

    def _mount_locked(self, canonical: str) -> None:
        """Compile routes and set mount state while holding ``_mount_lock``."""
        if self._mount_prefix:
            msg = f"router already mounted at '{self._mount_prefix}'"
            raise RuntimeError(msg)

        compiled_routes: list[_CompiledRoute] = []
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

    def attach(self, app: object, prefix: str | None = None) -> None:
        """Attach this router to a Falcon ASGI ``app``.

        Falcon's normal route table serves the WebSocket responder. The exact
        mount route handles a connection at the mount prefix and the path
        converter routes descendants through the same responder. If ``prefix``
        is omitted, call :meth:`mount` first. Passing a prefix retains the
        convenience form that mounts and attaches in one call. ``attach`` also
        configures Falcon's raw text and binary media handlers so
        ``receive_media()`` returns frames for :meth:`WebSocketResource.dispatch`.

        Parameters
        ----------
        app : object
            The Falcon ASGI application to attach to.
        prefix : str or None, default=None
            Optional mount prefix. If omitted, call :meth:`mount` first.

        Raises
        ------
        TypeError
            If ``app`` is not a Falcon ASGI application.
        RuntimeError
            If the router is not mounted, is already attached, or is mounted
            at a different prefix.
        """  # ruff: ignore[docstring-extraneous-exception] - delegated
        if not isinstance(app, falcon.asgi.App):
            msg = "attach() requires a falcon.asgi.App instance"
            raise TypeError(msg)
        attach_router(self, app, prefix)

    def add_route(
        self,
        path: str,
        resource: type[WebSocketResource] | cabc.Callable[..., WebSocketResource],
        *init_args: object,
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
        name = typ.cast("str | None", init_kwargs.pop("name", None))

        self._validate_resource_type(resource)
        path, canonical = self._registration.normalize_path(path)

        # Compile once to validate the template. The prefix is applied lazily
        # upon the first request since it may not yet be known at this point.
        compile_uri_template(canonical)

        factory = functools.partial(resource, *init_args, **init_kwargs)

        with self._mount_lock:
            self._registration.check_conflicts(canonical, name, path=path)
            compiled_route: _CompiledRoute | None = None
            if self._mount_prefix:
                compiled_route = self._compile_route(
                    canonical, factory, self._mount_prefix, self._routes
                )
            self._raw.append(_RawRoute(path, canonical, factory))
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
        self, req: _RequestLike, ws: WebSocketLike, **params: object
    ) -> None:  # pragma: no cover - simple wrapper
        """Dispatch the connection to the first matching route.

        Falcon passes parameters captured by its path converter as keyword
        arguments. They are ignored because the router re-matches ``req.path``
        against its own route table, preserving nested-resource and hook
        parameter handling. The selected Falcon template may be either the
        mount itself or its descendant catch-all route.

        Raises
        ------
        falcon.HTTPNotFound
            If the Falcon route template does not match this router's mount point,
            or if no registered route matches ``req.path``.
        """
        del params
        self._validate_mount_template(req)
        if await self._dispatch_first_route(req, ws):
            return
        raise falcon.HTTPNotFound

    def _validate_mount_template(self, req: _RequestLike) -> None:
        """Reject Falcon templates that are outside this router's mount."""
        template = _strip_terminal_greedy_path_field(
            _request_path_template(req).rstrip("/") or "/"
        )

        mount = self._mount_prefix.rstrip("/") or "/"
        if template != mount:
            msg = f"route template '{template}' does not match router mount '{mount}'"
            raise falcon.HTTPNotFound(description=msg)

    async def _dispatch_first_route(self, req: _RequestLike, ws: WebSocketLike) -> bool:
        """Dispatch the first route matching the request path."""
        # Routes are tested in the order they were added. Register more
        # specific paths before general ones to control precedence.
        for route in self._routes:
            if await self._try_route(route, req, ws):
                return True
        return False

    async def _try_route(
        self, route: _CompiledRoute, req: _RequestLike, ws: WebSocketLike
    ) -> bool:
        """Attempt to handle ``req`` using ``route``.

        The routing sequence is as follows:

        1. :meth:`_validate_and_normalize_path` ensures ``req.path`` matches the
           route's prefix and returns any captured parameters plus the remaining
           path.
        2. A base resource instance is created from the route's factory.
        3. :class:`_ResourceResolver` walks any nested subroutes on the base
           resource using the remaining path, merging parameters at each level.
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
                dispatch.session is not None and dispatch.session.has_established
            )
            factory_closed = getattr(exc, "_pachinko_factory_closed", False)
            if not session_is_established and not factory_closed:
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
        resolver = _ResourceResolver(self._resource_factory)
        base_resource = await resolver.instantiate(dispatch.route.factory, dispatch.ws)
        chain = [base_resource]
        resource = await resolver.resolve(
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
    def _validate_and_normalize_path(
        route: _CompiledRoute, req: _RequestLike
    ) -> _RouteMatch | None:
        """Return the path match for ``req`` or ``None`` if invalid."""
        if not (prefix_match := route.prefix.match(req.path)):
            return None
        remaining = req.path[prefix_match.end() :]
        if remaining and not remaining.startswith("/"):
            normalized = _normalize_path_remaining(remaining, prefix_match)
            if normalized is None:
                return None
            remaining = normalized
        return _RouteMatch(params=prefix_match.groupdict(), remaining=remaining)

    async def _handle_websocket_connection(
        self,
        resource: WebSocketResource,
        dispatch: _Dispatch,
        *,
        hook_manager: HookManager,
    ) -> bool:
        """Run connection setup, then own the accepted session until closure."""
        with self._mount_lock:
            self._session_started = True
            instrumentation = self._session_instrumentation
            session_clock = self._session_clock
        return await run_session(
            resource,
            dispatch,
            hook_manager=hook_manager,
            options=_SessionOptions(
                receive_loop=self._receive_loop,
                observation_clock=session_clock,
                instrumentation_clock=session_clock,
                instrumentation=instrumentation,
            ),
        )
