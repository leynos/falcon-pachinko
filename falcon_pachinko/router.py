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
import typing

import falcon

if typing.TYPE_CHECKING:
    from .resource import WebSocketLike, WebSocketResource


def compile_uri_template(template: str) -> re.Pattern[str]:
    """Compile a simple URI template into a regex pattern."""

    def replace_param(match: re.Match[str]) -> str:
        param_name = match.group(1)
        if not param_name:
            msg = f"Empty parameter name in template: {template}"
            raise ValueError(msg)
        return f"(?P<{param_name}>[^/]+)"

    pattern = re.sub(r"{([^}]*)}", replace_param, template.rstrip("/"))
    pattern = f"^{pattern}/?$"
    return re.compile(pattern)


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
        factory: typing.Callable[..., WebSocketResource]

    @dc.dataclass
    class _CompiledRoute:
        pattern: re.Pattern[str]
        factory: typing.Callable[..., WebSocketResource]

    def __init__(self, *, name: str | None = None) -> None:
        self._raw: list[WebSocketRouter._RawRoute] = []
        self._routes: list[WebSocketRouter._CompiledRoute] = []
        self._mount_prefix: str = ""
        self._mount_lock = threading.Lock()
        self._names: dict[str, str] = {}
        self.name = name

    def _compile_and_store_route(
        self,
        canonical: str,
        factory: typing.Callable[..., WebSocketResource],
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
        for existing in self._routes:
            if existing.pattern.pattern == pattern.pattern:
                msg = f"route path {full!r} already registered"
                raise ValueError(msg)

        self._routes.append(WebSocketRouter._CompiledRoute(pattern, factory))

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
        resource: type[WebSocketResource] | typing.Callable[..., WebSocketResource],
        *,
        name: str | None = None,
        args: tuple[typing.Any, ...] = (),
        kwargs: dict[str, typing.Any] | None = None,
    ) -> None:
        """Register a WebSocketResource to handle ``path``."""
        if kwargs is None:
            kwargs = {}

        if not callable(resource):
            msg = "resource must be callable"
            raise TypeError(msg)

        path = _normalize_path(path)
        canonical = _canonical_path(path)
        if any(r.canonical == canonical for r in self._raw):
            msg = f"route path {path!r} already registered"
            raise ValueError(msg)
        if name and name in self._names:
            msg = f"route name {name!r} already registered"
            raise ValueError(msg)

        # Compile once to validate the template. The prefix is applied lazily
        # upon the first request since it may not yet be known at this point.
        compile_uri_template(canonical)

        factory = functools.partial(resource, *args, **kwargs)

        with self._mount_lock:
            self._raw.append(WebSocketRouter._RawRoute(path, canonical, factory))
            if self._mount_prefix:
                self._compile_and_store_route(canonical, factory)
        if name:
            self._names[name] = path

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
            if match := route.pattern.fullmatch(req.path):
                try:
                    resource = route.factory()
                    should_accept = await resource.on_connect(
                        req, ws, **match.groupdict()
                    )
                except Exception:
                    await ws.close()
                    raise

                if not should_accept:
                    await ws.close()
                    return

                await ws.accept()
                return

        raise falcon.HTTPNotFound
