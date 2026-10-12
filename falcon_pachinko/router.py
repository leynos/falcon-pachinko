"""WebSocket routing utilities.

This module implements :class:`WebSocketRouter`, a Falcon resource that
dispatches incoming WebSocket connections based on path templates. Helper
functions handle template compilation and path normalization to support
both trailing and non-trailing slashes. The router can be mounted within a
Falcon app and used to generate URLs for registered routes.
"""

from __future__ import annotations

import collections.abc as cabc
import dataclasses as dc
import functools
import re
import threading
import typing as typ

import falcon

from ._router_lifecycle import (
    _ConnectionPreparation,
    _Dispatch,
    execute_route_with_error_handling,
)
from ._router_registration import _RouteRegistrationService
from ._router_resolution import _RouterResolutionMixin
from ._uri_template import _compile_prefix_template, compile_uri_template
from .hooks import HookCollection, HookManager

# Keep this public return annotation available to runtime introspection.
from .lifecycle import WebSocketSession  # ruff: ignore[typing-only-first-party-import]
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


class _RequestLike(typ.Protocol):
    """Request surface consumed by the router before resource dispatch."""

    @property
    def path(self) -> str:
        """The request path."""


def _request_path_template(req: _RequestLike) -> str:
    """Return the mounted path template, defaulting like Falcon does."""
    template = getattr(req, "path_template", None)
    if template is None:
        template = getattr(req, "uri_template", None)
    if template is None:
        template = ""

    # Falcon does not treat a `{name:path}` route as a mount prefix. The
    # descendant route therefore has to be reduced to the mounted identity.
    match = re.fullmatch(r"(?P<prefix>.*)/\{[A-Za-z_]\w*:path\}", template)
    if match is not None:
        return match.group("prefix") or "/"
    return template


def _normalize_path(path: str) -> str:
    """Ensure ``path`` has a leading slash."""
    return path if path.startswith("/") else f"/{path}"


def _canonical_path(path: str) -> str:
    """Return the normalized path without a trailing slash."""
    normalized = _normalize_path(path)
    return normalized.rstrip("/") if normalized != "/" else normalized


class WebSocketRouter(_RouterResolutionMixin):
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
        path = _normalize_path(path)
        canonical = _canonical_path(path)

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
        """Negotiate a connection, then own its session through disconnect."""
        session = await self.negotiate(req, ws)
        if session is not None:
            await session.run()

    async def negotiate(
        self, req: _RequestLike, ws: WebSocketLike, **_params: object
    ) -> WebSocketSession | None:
        """Run route and pre-accept callbacks, returning an accepted session.

        ``on_connect`` and ``after_connect`` run before router acceptance.
        Returning a falsey value rejects the HTTP upgrade. Returning ``True``
        accepts with defaults; ``AcceptOptions`` supplies the subprotocol and
        headers. A returned session is already accepted but has not run
        ``on_session_start``; call :meth:`WebSocketSession.run` to own its
        receive loop, or dispatch manually through ``session.resource``.

        Returns
        -------
        WebSocketSession or None
            The accepted session handle, or ``None`` when negotiation rejects.

        Raises
        ------
        falcon.HTTPNotFound
            If the request does not match this router's mount point or routes.
        """
        self._validate_mount(req)
        preparation = _ConnectionPreparation()
        for route in self._routes:
            matched, session = await self._negotiate_route(route, req, ws, preparation)
            if matched:
                return session
        raise falcon.HTTPNotFound

    def _validate_mount(self, req: _RequestLike) -> None:
        """Raise ``HTTPNotFound`` when the mounted request path is unrelated."""
        prefix = _request_path_template(req).rstrip("/") or "/"
        if prefix != self._mount_prefix:
            msg = (
                f"path_template '{prefix}' does not match router mount "
                f"'{self._mount_prefix}'"
            )
            raise falcon.HTTPNotFound(description=msg)

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
        4. If a final resource and empty path are resolved, the router runs
           pre-accept negotiation and returns an accepted session handle.

        Returns
        -------
        bool
            ``True`` if the request was handled by this route, ``False``
            otherwise.
        """
        matched, session = await self._negotiate_route(
            route, req, ws, _ConnectionPreparation()
        )
        if session is not None:
            await session.run()
        return matched

    async def _negotiate_route(
        self,
        route: _CompiledRoute,
        req: _RequestLike,
        ws: WebSocketLike,
        preparation: _ConnectionPreparation,
    ) -> tuple[bool, WebSocketSession | None]:
        """Resolve one candidate route and negotiate its WebSocket session."""
        match = self._validate_and_normalize_path(route, req)
        if match is None:
            return False, None
        # cast: downstream hooks and ``on_connect`` are typed against
        # ``falcon.Request``; the router accepts any ``_RequestLike`` so tests
        # can supply structural stand-ins.
        dispatch = _Dispatch(
            route=route,
            req=typ.cast("falcon.Request", req),
            ws=(ws if preparation.websocket is None else preparation.websocket),
            match=match,
            session=preparation.session,
        )
        session = await execute_route_with_error_handling(self, dispatch, preparation)
        return dispatch.matched, session

    def _setup_hook_management(
        self, chain: list[WebSocketResource], session: SessionState
    ) -> HookManager:
        """Attach a :class:`HookManager` to every resource in ``chain``."""
        manager = HookManager(global_hooks=self.global_hooks, resources=chain)
        manager.bind_session(session)
        for item in chain:
            item.bind_hook_manager(manager)
        return manager
