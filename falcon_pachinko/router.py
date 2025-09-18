"""WebSocket routing utilities.

This module implements :class:`WebSocketRouter`, a Falcon resource that
dispatches incoming WebSocket connections based on path templates. Helper
functions handle template compilation and path normalization to support
both trailing and non-trailing slashes. The router can be mounted within a
Falcon app and used to generate URLs for registered routes.
"""

from __future__ import annotations

import dataclasses as dc
import functools
import re
import threading
import typing as typ

import falcon

from .hooks import HookCollection, HookContext, HookManager

if typ.TYPE_CHECKING:
    from .protocols import WebSocketLike
    from .resource import WebSocketResource


def _replace_param_in_template(match: re.Match[str], template: str) -> str:
    """Return a regex group for ``match`` ensuring the param is non-empty."""
    param_name = match.group(1)
    if not param_name:
        msg = f"Empty parameter name in template: {template}"
        raise ValueError(msg)
    return f"(?P<{param_name}>[^/]+)"


def _compile_template_with_suffix(template: str, suffix: str) -> re.Pattern[str]:
    """Compile ``template`` with ``suffix`` appended."""
    pattern = re.sub(
        r"{([^}]*)}",
        functools.partial(_replace_param_in_template, template=template),
        template.rstrip("/"),
    )
    pattern = f"^{pattern}{suffix}"
    return re.compile(pattern)


def compile_uri_template(template: str) -> re.Pattern[str]:
    """Compile a simple URI template into a regex pattern."""
    return _compile_template_with_suffix(template, "/?$")


def _compile_prefix_template(template: str) -> re.Pattern[str]:
    """Compile ``template`` to match a path prefix."""
    return _compile_template_with_suffix(template, "(?:/|$)")


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


class WebSocketRouter:
    """Route WebSocket connections to resources.

    Routes are evaluated in the order they were added. If multiple patterns
    overlap, the first match wins. Register more specific paths before more
    general ones to control precedence. Paths are normalized so that a template
    ``"/foo"`` matches ``"/foo"`` and ``"/foo/"`` equally. If a trailing slash is
    included in the template, generated URLs will preserve it.
    """

    @dc.dataclass
    class _RawRoute:
        template: str
        canonical: str
        factory: typ.Callable[..., WebSocketResource]

    @dc.dataclass
    class _CompiledRoute:
        prefix: re.Pattern[str]
        pattern: re.Pattern[str]
        factory: typ.Callable[..., WebSocketResource]

    def __init__(self, *, name: str | None = None) -> None:
        self._raw: list[WebSocketRouter._RawRoute] = []
        self._routes: list[WebSocketRouter._CompiledRoute] = []
        self._mount_prefix: str = ""
        self._mount_lock = threading.Lock()
        self._names: dict[str, str] = {}
        self.global_hooks = HookCollection()
        self.name = name

    def _compile_and_store_route(
        self,
        canonical: str,
        factory: typ.Callable[..., WebSocketResource],
    ) -> None:
        """Compile ``canonical`` with the mount prefix and store it.

        This helper mutates :attr:`_routes` and therefore assumes the caller
        already holds :attr:`_mount_lock`. The router relies on this lock to
        guard all mount-related state, preventing race conditions when routes
        are added concurrently with mounting.
        """
        base = self._mount_prefix.rstrip("/")
        full = f"{base}{canonical}"
        pattern = compile_uri_template(full)
        prefix = _compile_prefix_template(full)
        for existing in self._routes:
            if existing.pattern.pattern == pattern.pattern:
                msg = f"route path {full!r} already registered"
                raise ValueError(msg)

        self._routes.append(WebSocketRouter._CompiledRoute(prefix, pattern, factory))

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

            self._mount_prefix = canonical
            for raw in self._raw:
                self._compile_and_store_route(raw.canonical, raw.factory)

    def add_route(
        self,
        path: str,
        resource: type[WebSocketResource] | typ.Callable[..., WebSocketResource],
        *,
        name: str | None = None,
        args: tuple[typ.Any, ...] = (),
        kwargs: dict[str, typ.Any] | None = None,
    ) -> None:
        """Register a WebSocketResource to handle ``path``."""
        if kwargs is None:
            kwargs = {}

        self._validate_resource_type(resource)
        path, canonical = self._normalize_route_path(path)

        # Compile once to validate the template. The prefix is applied lazily
        # upon the first request since it may not yet be known at this point.
        compile_uri_template(canonical)

        factory = functools.partial(resource, *args, **kwargs)

        with self._mount_lock:
            self._check_route_conflicts(canonical, name, path)
            self._raw.append(WebSocketRouter._RawRoute(path, canonical, factory))
            if name:
                self._names[name] = path
            if self._mount_prefix:
                self._compile_and_store_route(canonical, factory)

    def _validate_resource_type(
        self, resource: type[WebSocketResource] | typ.Callable[..., WebSocketResource]
    ) -> None:
        """Ensure ``resource`` can be called to create a handler."""
        if not callable(resource):
            msg = "resource must be callable"
            raise TypeError(msg)

    def _normalize_route_path(self, path: str) -> tuple[str, str]:
        """Return normalized and canonical variants of ``path``."""
        normalized = _normalize_path(path)
        canonical = _canonical_path(normalized)
        return normalized, canonical

    def _check_route_conflicts(
        self, canonical: str, name: str | None, path: str | None = None
    ) -> None:
        """Raise if ``canonical`` or ``name`` already exists.

        Callers must hold :attr:`_mount_lock` while invoking this helper to
        avoid racing concurrent registrations.
        """
        if not self._mount_lock.locked():
            msg = "_check_route_conflicts requires _mount_lock to be held"
            raise RuntimeError(msg)

        display_path = path if path is not None else canonical
        if any(r.canonical == canonical for r in self._raw):
            msg = f"route path {display_path!r} already registered"
            raise ValueError(msg)
        if name and name in self._names:
            msg = f"route name {name!r} already registered"
            raise ValueError(msg)

    def url_for(self, name: str, **params: object) -> str:
        """Return the URL path associated with ``name`` formatted with ``params``."""
        try:
            template = self._names[name]
        except KeyError as exc:
            msg = f"no route registered with name {name!r}"
            raise KeyError(msg) from exc

        return _normalize_path(template.format(**params))

    async def on_websocket(
        self, req: falcon.Request, ws: WebSocketLike
    ) -> None:  # pragma: no cover - simple wrapper
        """Dispatch the connection to the first matching route.

        ``req.path_template`` is assumed to be a prefix of ``req.path``. If the
        assumption fails, :class:`falcon.HTTPNotFound` is raised to signal that
        the requested path does not map to this router's mount point.
        """
        # Handle missing or empty path_template by defaulting to root "/"
        prefix = getattr(req, "path_template", "").rstrip("/") or "/"
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
        self, route: _CompiledRoute, req: falcon.Request, ws: WebSocketLike
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

        The method returns ``True`` if the request was handled by this route,
        ``False`` otherwise.
        """
        result = self._validate_and_normalize_path(route, req)
        if result is None:
            return False
        params, remaining = result
        return await self._execute_route_with_error_handling(
            route, req, ws, params, remaining
        )

    async def _execute_route_with_error_handling(
        self,
        route: _CompiledRoute,
        req: falcon.Request,
        ws: WebSocketLike,
        params: dict[str, str],
        remaining: str,
    ) -> bool:
        """Run the routing pipeline and close ``ws`` on unexpected errors."""
        try:
            return await self._process_route_resolution(
                route, req, ws, params, remaining
            )
        except Exception:
            await ws.close()
            raise

    async def _process_route_resolution(
        self,
        route: _CompiledRoute,
        req: falcon.Request,
        ws: WebSocketLike,
        params: dict[str, str],
        remaining: str,
    ) -> bool:
        """Resolve the final resource and dispatch the connection."""
        base_resource = route.factory()
        chain = [base_resource]
        resolution = self._resolve_resource_and_path(
            base_resource, remaining, params, chain
        )
        if resolution is None:
            return False
        resource, _, params, chain = resolution
        if not self._validate_final_resource(resource, base_resource, route, req):
            return False
        manager = self._setup_hook_management(chain)
        return await self._handle_websocket_connection(
            resource, req, ws, params, hook_manager=manager
        )

    def _validate_final_resource(
        self,
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
            item._hook_manager = manager  # type: ignore[attr-defined]
        return manager

    def _normalize_path_remaining(
        self, remaining: str, match: re.Match[str]
    ) -> str | None:
        """Normalize ``remaining`` or return ``None`` if invalid."""
        if not remaining or remaining.startswith("/"):
            return remaining
        return f"/{remaining}" if match.group(0).endswith("/") else None

    def _try_subroute_match(
        self, resource: WebSocketResource, path: str
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
                new_resource = factory(**child_kwargs)
                new_resource.state = context.get("state", resource.state)
                params = match.groupdict()
                return new_resource, remaining, params
        return None

    def _resolve_subroutes(
        self,
        resource: WebSocketResource,
        path: str,
        params: dict[str, str],
        chain: list[WebSocketResource],
    ) -> tuple[WebSocketResource, str, dict[str, str]]:
        """Traverse ``resource`` subroutes matching ``path``."""
        while path not in ("", "/"):
            result = self._try_subroute_match(resource, path)
            if result is None:
                break
            resource, path, new_params = result
            params |= new_params
            chain.append(resource)

        return resource, path, params

    def _validate_and_normalize_path(
        self, route: _CompiledRoute, req: falcon.Request
    ) -> tuple[dict[str, str], str] | None:
        """Return params and remaining path or ``None`` if invalid."""
        if not (match := route.prefix.match(req.path)):
            return None
        params = match.groupdict()
        remaining = req.path[match.end() :]
        if remaining and not remaining.startswith("/"):
            remaining = self._normalize_path_remaining(remaining, match)
            if remaining is None:
                return None
        return params, remaining

    def _resolve_resource_and_path(
        self,
        resource: WebSocketResource,
        remaining: str,
        params: dict[str, str],
        chain: list[WebSocketResource],
    ) -> tuple[WebSocketResource, str, dict[str, str], list[WebSocketResource]] | None:
        """Return resolved resource, params, and traversal chain."""
        resolved, remaining, params = self._resolve_subroutes(
            resource, remaining, params, chain
        )
        return (resolved, remaining, params, chain) if remaining in ("", "/") else None

    async def _handle_websocket_connection(
        self,
        resource: WebSocketResource,
        req: falcon.Request,
        ws: WebSocketLike,
        params: dict[str, str],
        *,
        hook_manager: HookManager,
    ) -> bool:
        """Accept or close ``ws`` based on ``resource`` decision."""
        context, params_for_handler = await self._prepare_connection_context(
            hook_manager, resource, req, ws, params
        )
        should_accept = await self._execute_resource_handler(
            resource, req, ws, params_for_handler, context, hook_manager
        )
        context.params = params_for_handler
        return await self._finalize_connection(
            ws,
            should_accept=should_accept,
            context=context,
            hook_manager=hook_manager,
        )

    async def _prepare_connection_context(
        self,
        hook_manager: HookManager,
        resource: WebSocketResource,
        req: falcon.Request,
        ws: WebSocketLike,
        params: dict[str, str],
    ) -> tuple[HookContext, dict[str, object]]:
        """Return the hook context and handler parameters."""
        params_obj: dict[str, object] = dict(params)
        context = await hook_manager.notify_before_connect(
            resource, req=req, ws=ws, params=params_obj
        )
        params_for_handler = (
            context.params if context.params is not None else params_obj
        )
        return context, params_for_handler

    async def _execute_resource_handler(
        self,
        resource: WebSocketResource,
        req: falcon.Request,
        ws: WebSocketLike,
        params: dict[str, object],
        context: HookContext,
        hook_manager: HookManager,
    ) -> bool:
        """Invoke ``resource.on_connect`` handling hook error propagation."""
        try:
            return await resource.on_connect(req, ws, **params)
        except Exception as exc:
            context.error = exc
            context.result = False
            context.params = params
            await hook_manager.notify_after_connect(context)
            raise

    async def _finalize_connection(
        self,
        ws: WebSocketLike,
        *,
        should_accept: bool,
        context: HookContext,
        hook_manager: HookManager,
    ) -> bool:
        """Complete the connection lifecycle and honour ``should_accept``."""
        context.result = bool(should_accept)
        await hook_manager.notify_after_connect(context)
        if not should_accept:
            await ws.close()
            return True
        await ws.accept()
        return True
