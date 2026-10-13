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
import typing as typ

import falcon
import falcon.asgi

from ._router_lifecycle import (
    _AcceptedConnection,
    _ConnectionFinalization,
    _Dispatch,
    _FalconResponderAdapter,
    _request_path_template,
    _RequestLike,
    _RouteAttempt,
    _TextReceivingWebSocket,
)
from ._uri_template import _compile_prefix_template, compile_uri_template
from .hooks import HookCollection, HookContext, HookManager
from .protocols import WebSocketLike

# Imported at runtime, not under TYPE_CHECKING: ``ResourceFactory`` below is a
# PEP 695 alias whose value is evaluated lazily on access. Exporting an alias
# that raises NameError when introspected would be a trap for runtime
# consumers, so the name it references has to resolve at runtime too.
from .resource import WebSocketResource

if typ.TYPE_CHECKING:
    import re

__all__ = ["ResourceFactory", "SimulatorFactory", "WebSocketRouter"]


type ResourceFactory = cabc.Callable[
    [cabc.Callable[..., WebSocketResource]],
    WebSocketResource,
]

type SimulatorFactory = cabc.Callable[
    [falcon.Request, WebSocketLike],
    WebSocketLike | cabc.Awaitable[WebSocketLike],
]


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
        self._attached = False
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

    def attach(self, app: falcon.asgi.App) -> None:
        """Register the router's Falcon responder at its mounted prefix.

        Call :meth:`mount` before attaching the router. The responder handles
        both the prefix itself and every nested path below it.

        Raises
        ------
        RuntimeError
            If the router has not been mounted or has already been attached.
        """
        with self._mount_lock:
            if not self._mount_prefix:
                msg = "router must be mounted before it can be attached"
                raise RuntimeError(msg)
            if self._attached:
                msg = "router is already attached to a Falcon app"
                raise RuntimeError(msg)

            prefix = self._mount_prefix.rstrip("/")
            prefix_template = prefix or "/"
            greedy_template = self._greedy_route_template(prefix)
            adapter = _FalconResponderAdapter(self)
            app.add_route(prefix_template, adapter)
            app.add_route(greedy_template, adapter)
            self._attached = True

    def _greedy_route_template(self, prefix: str) -> str:
        """Build the nested Falcon route with an unused private field name."""
        used_names = set(compile_uri_template(self._mount_prefix).groupindex)
        for route in self._raw:
            used_names.update(compile_uri_template(route.template).groupindex)

        field_name = "_falcon_pachinko_path"
        while field_name in used_names:
            field_name += "_"
        if not prefix:
            return f"/{{{field_name}:path}}"
        return f"{prefix}/{{{field_name}:path}}"

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
        self, req: _RequestLike, ws: WebSocketLike
    ) -> None:  # pragma: no cover - simple wrapper
        """Set up the first matching resource without owning its receive loop.

        This setup-only entry point remains useful for direct simulator tests.
        Falcon applications should call :meth:`attach` so the responder adapter
        can also receive and dispatch text frames for the accepted connection.

        """
        await self._setup_websocket(req, ws)

    async def _setup_websocket(
        self, req: _RequestLike, ws: WebSocketLike
    ) -> _AcceptedConnection | None:
        """Resolve, accept, and initialise a matching resource if permitted."""
        prefix = _request_path_template(req).rstrip("/") or "/"
        if prefix != self._mount_prefix:
            msg = (
                f"request template '{prefix}' does not match router mount "
                f"'{self._mount_prefix}'"
            )
            raise falcon.HTTPNotFound(description=msg)

        for route in self._routes:
            attempt = await self._try_route_setup(route, req, ws)
            if attempt.matched:
                return attempt.connection

        raise falcon.HTTPNotFound

    async def _drive_accepted_connection(self, connection: _AcceptedConnection) -> None:
        """Receive text frames until disconnect, dispatching them in order."""
        resource, ws = connection.resource, connection.ws
        try:
            text_ws = typ.cast("_TextReceivingWebSocket", ws)
            while True:
                raw = await text_ws.receive_text()
                await resource.dispatch(ws, raw)
        except falcon.WebSocketDisconnected as disconnected:
            await self._finalize_disconnect(connection, disconnected.code)
        except asyncio.CancelledError as error:
            await self._handle_post_accept_error(connection, error)
            raise
        except Exception as error:
            await self._handle_post_accept_error(connection, error)
            raise

    async def _try_route(
        self, route: _CompiledRoute, req: _RequestLike, ws: WebSocketLike
    ) -> bool:
        """Return whether ``route`` resolves and handles ``req``."""
        return (await self._try_route_setup(route, req, ws)).matched

    async def _try_route_setup(
        self, route: _CompiledRoute, req: _RequestLike, ws: WebSocketLike
    ) -> _RouteAttempt:
        """Attempt setup and return its match and accepted connection state.

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
        _RouteAttempt
            Whether this route handled the path and, when accepted, the
            resource and effective socket for the responder loop.
        """
        match = self._validate_and_normalize_path(route, req)
        if match is None:
            return _RouteAttempt(matched=False)
        # cast: downstream hooks and ``on_connect`` are typed against
        # ``falcon.Request``; the router accepts any ``_RequestLike`` so tests
        # can supply structural stand-ins.
        return await self._execute_route_with_error_handling(
            _Dispatch(
                route=route, req=typ.cast("falcon.Request", req), ws=ws, match=match
            )
        )

    async def _execute_route_with_error_handling(
        self, dispatch: _Dispatch
    ) -> _RouteAttempt:
        """Run the routing pipeline and close pre-acceptance errors."""
        ws_for_cleanup: WebSocketLike = dispatch.ws
        try:
            dispatch.ws = await self._prepare_websocket(dispatch.req, dispatch.ws)
            ws_for_cleanup = dispatch.ws
            return await self._process_route_resolution(dispatch)
        except Exception as exc:
            if not dispatch.accepted and not getattr(
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

    async def _process_route_resolution(self, dispatch: _Dispatch) -> _RouteAttempt:
        """Resolve the final resource and perform connection setup."""
        base_resource = await self._instantiate_resource(
            dispatch.route.factory, dispatch.ws
        )
        chain = [base_resource]
        resource = await self._resolve_resource_and_path(
            base_resource, dispatch.match, chain, dispatch.ws
        )
        if resource is None:
            return _RouteAttempt(matched=False)
        if not self._validate_final_resource(
            resource, base_resource, dispatch.route, dispatch.req
        ):
            return _RouteAttempt(matched=False)
        manager = self._setup_hook_management(chain)
        connection = await self._handle_websocket_connection(
            resource, dispatch, hook_manager=manager
        )
        return _RouteAttempt(matched=True, connection=connection)

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
    ) -> _AcceptedConnection | None:
        """Accept or close the connection based on ``resource`` decision."""
        context = await self._prepare_connection_context(
            hook_manager, resource, dispatch
        )
        should_accept = await self._execute_resource_handler(
            resource, dispatch, context, hook_manager
        )
        return await self._finalize_connection(
            resource,
            _ConnectionFinalization(
                dispatch=dispatch,
                should_accept=should_accept,
                context=context,
                hook_manager=hook_manager,
            ),
        )

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
            await hook_manager.notify_after_connect(context)
            raise

    async def _finalize_connection(
        self,
        resource: WebSocketResource,
        finalization: _ConnectionFinalization,
    ) -> _AcceptedConnection | None:
        """Complete the connection lifecycle and honour ``should_accept``."""
        dispatch = finalization.dispatch
        ws = dispatch.ws
        context = finalization.context
        hook_manager = finalization.hook_manager
        should_accept = finalization.should_accept
        context.result = should_accept
        await hook_manager.notify_after_connect(context)
        if not should_accept:
            await ws.close()
            return None

        connection = _AcceptedConnection(resource, ws, hook_manager)
        await ws.accept()
        dispatch.accepted = True
        try:
            await resource.on_accepted(ws)
        except falcon.WebSocketDisconnected as disconnected:
            await self._finalize_disconnect(connection, disconnected.code)
            return None
        except asyncio.CancelledError as error:
            await self._handle_post_accept_error(connection, error)
            raise
        except Exception as error:
            await self._handle_post_accept_error(connection, error)
            raise
        return connection

    async def _handle_post_accept_error(
        self, connection: _AcceptedConnection, error: BaseException
    ) -> None:
        """Close failed sessions and preserve their primary error."""
        close_code = 1006
        if not isinstance(error, asyncio.CancelledError):
            close_code = 1011
            with contextlib.suppress(Exception):
                await connection.ws.close(code=close_code)
        await self._finalize_disconnect_preserving_error(
            connection, close_code=close_code, primary_error=error
        )

    @staticmethod
    async def _finalize_disconnect(
        connection: _AcceptedConnection, close_code: int
    ) -> None:
        """Run disconnect hooks before resource cleanup, even if a hook fails."""
        error: BaseException | None = None
        try:
            await connection.hook_manager.notify_before_disconnect(
                connection.resource,
                ws=connection.ws,
                close_code=close_code,
            )
        # A failing hook must not prevent the resource cleanup callback.
        except (asyncio.CancelledError, Exception) as exc:  # ruff: ignore[blind-except]
            error = exc

        try:
            await connection.resource.on_disconnect(connection.ws, close_code)
        # Preserve the first failure while still completing remaining cleanup.
        except (asyncio.CancelledError, Exception) as exc:  # ruff: ignore[blind-except]
            if error is None:
                error = exc
            else:
                error.add_note(
                    f"Resource disconnect cleanup also raised {type(exc).__name__}"
                )

        if error is not None:
            raise error

    @classmethod
    async def _finalize_disconnect_preserving_error(
        cls,
        connection: _AcceptedConnection,
        *,
        close_code: int,
        primary_error: BaseException,
    ) -> None:
        """Complete disconnect callbacks without replacing a primary failure."""
        try:
            await cls._finalize_disconnect(connection, close_code)
        # Cleanup errors should not replace the handler or cancellation error.
        except (asyncio.CancelledError, Exception) as cleanup_error:  # ruff: ignore[blind-except]
            primary_error.add_note(
                f"Disconnect finalization also raised {type(cleanup_error).__name__}"
            )


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
