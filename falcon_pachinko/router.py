"""WebSocket routing utilities.

This module implements :class:`WebSocketRouter`, a Falcon resource that
dispatches incoming WebSocket connections based on path templates. Helper
functions handle template compilation and path normalization to support
both trailing and non-trailing slashes. The router can be mounted within a
Falcon app and used to generate URLs for registered routes.
"""

from __future__ import annotations

import collections.abc as cabc
import functools
import threading
import typing as typ

import falcon
import falcon.asgi

from ._observability import _observe_lifecycle
from ._route_support import (
    _CompiledRoute,
    _normalize_path,
    _RawRoute,
    _request_path_template,
    _RequestLike,
    _RouteRegistrationService,
    _strip_terminal_greedy_path_field,
)
from ._router_attachment import attach_router
from ._router_lifecycle import (
    _ConnectionPreparation,
    _Dispatch,
    execute_route_with_error_handling,
)
from ._router_resolution import _RouterResolutionMixin
from ._uri_template import _compile_prefix_template, compile_uri_template
from .hooks import HookCollection, HookManager
from .lifecycle import ConnectionPhase, WebSocketSession
from .protocols import WebSocketLike

# Imported at runtime, not under TYPE_CHECKING: ``ResourceFactory`` below is a
# PEP 695 alias whose value is evaluated lazily on access. Exporting an alias
# that raises NameError when introspected would be a trap for runtime
# consumers, so the name it references has to resolve at runtime too.
from .resource import WebSocketResource

if typ.TYPE_CHECKING:
    from ._session import SessionState
    from .observability import LifecycleObserver

__all__ = ["ResourceFactory", "SimulatorFactory", "WebSocketRouter"]


type ResourceFactory = cabc.Callable[
    [cabc.Callable[..., WebSocketResource]],
    WebSocketResource,
]

type SimulatorFactory = cabc.Callable[
    [falcon.Request, WebSocketLike],
    WebSocketLike | cabc.Awaitable[WebSocketLike],
]


class WebSocketRouter(_RouterResolutionMixin):
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
    ) -> None:
        """Create a router and configure its resource factories.

        Parameters
        ----------
        name : str or None, default=None
            Optional router name used by route registration.
        resource_factory : ResourceFactory or None, default=None
            Optional factory used to create routed resources.
        simulator_factory : SimulatorFactory or None, default=None
            Optional factory used by simulator routing.
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
        self._attached = False
        self._lifecycle_observer: LifecycleObserver | None = None

    @property
    def lifecycle_observer(self) -> LifecycleObserver | None:
        """Optional host adapter for lifecycle metrics and tracing.

        Returns
        -------
        LifecycleObserver or None
            The configured adapter, or ``None`` when instrumentation is disabled.
        """
        return self._lifecycle_observer

    @lifecycle_observer.setter
    def lifecycle_observer(self, observer: LifecycleObserver | None) -> None:
        """Configure optional lifecycle instrumentation before serving requests.

        Parameters
        ----------
        observer : LifecycleObserver or None
            Synchronous host adapter, or ``None`` to disable instrumentation.
            Existing sessions retain the adapter selected during negotiation.
        """
        self._lifecycle_observer = observer

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
    ) -> None:
        """Negotiate and run the session for the first matching route.

        Falcon's path parameters are ignored because the router re-matches
        ``req.path`` and builds captures across nested resources.
        """
        session = await self.negotiate(req, ws, **params)
        if session is not None:
            await session.run()

    async def negotiate(
        self, req: _RequestLike, ws: WebSocketLike, **params: object
    ) -> WebSocketSession | None:
        """Run pre-accept routing and return the accepted session handle.

        ``on_connect`` and ``after_connect`` run before router acceptance. A
        falsey decision rejects the upgrade; ``True`` accepts with defaults,
        and ``AcceptOptions`` supplies the subprotocol and response headers.
        The returned session is accepted but has not run
        ``on_session_start``. Call :meth:`WebSocketSession.run` to own its
        receive loop, or call :meth:`WebSocketSession.aclose` when dispatch is
        managed manually.

        Parameters
        ----------
        req : _RequestLike
            The incoming request.
        ws : WebSocketLike
            The WebSocket transport supplied by Falcon.
        **params : object
            Falcon route parameters, ignored because the router re-matches the
            request path and builds its own nested-resource captures.

        Returns
        -------
        WebSocketSession or None
            The accepted session, or ``None`` when the selected resource rejects.
        """
        del params
        preparation = _ConnectionPreparation(observer=self._lifecycle_observer)
        with _observe_lifecycle(
            preparation.observer,
            "negotiation",
            span_name="falcon_pachinko.websocket.negotiation",
        ) as observation:
            try:
                self._validate_mount_template(req)
                session = await self._negotiate_route(req, ws, preparation)
                if session is None:
                    observation.reject()
                return session
            finally:
                observation.set_close_code(preparation.session.close_code)

    async def _negotiate_route(
        self,
        req: _RequestLike,
        ws: WebSocketLike,
        preparation: _ConnectionPreparation,
    ) -> WebSocketSession | None:
        """Resolve route candidates using one guarded transport and observer."""
        for route in self._routes:
            match = self._validate_and_normalize_path(route, req)
            if match is None:
                continue
            dispatch = _Dispatch(
                route=route,
                req=typ.cast("falcon.Request", req),
                ws=ws if preparation.websocket is None else preparation.websocket,
                match=match,
                session=preparation.session,
            )
            session = await execute_route_with_error_handling(
                self, dispatch, preparation
            )
            if dispatch.matched:
                return session

        if (
            preparation.websocket is not None
            and preparation.websocket.phase is ConnectionPhase.PRE_ACCEPT
        ):
            await preparation.websocket.close()
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

    async def _try_route(
        self, route: _CompiledRoute, req: _RequestLike, ws: WebSocketLike
    ) -> bool:
        """Attempt one route and run its accepted session when it matches."""
        match = self._validate_and_normalize_path(route, req)
        if match is None:
            return False
        preparation = _ConnectionPreparation(observer=self._lifecycle_observer)
        dispatch = _Dispatch(
            route=route,
            req=typ.cast("falcon.Request", req),
            ws=ws,
            match=match,
            session=preparation.session,
        )
        session = await execute_route_with_error_handling(self, dispatch, preparation)
        if session is not None:
            await session.run()
        elif not dispatch.matched:
            websocket = preparation.websocket
            if websocket is not None and websocket.phase is ConnectionPhase.PRE_ACCEPT:
                await websocket.close()
        return dispatch.matched

    def _setup_hook_management(
        self, chain: list[WebSocketResource], session: SessionState
    ) -> HookManager:
        """Bind shared session state to hooks and each nested resource."""
        manager = HookManager(global_hooks=self.global_hooks, resources=chain)
        manager.bind_session(session)
        for item in chain:
            item.bind_hook_manager(manager)
        return manager

    async def _instantiate_resource(
        self,
        route_factory: cabc.Callable[..., WebSocketResource],
        ws: WebSocketLike,
    ) -> WebSocketResource:
        """Instantiate a resource; the route pipeline owns failure cleanup."""
        del ws
        return self._resource_factory(route_factory)
