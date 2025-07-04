"""WebSocket routing utilities."""

from __future__ import annotations

import functools
import re
import typing

import falcon

if typing.TYPE_CHECKING:
    from .resource import WebSocketLike, WebSocketResource


def _compile_template(template: str) -> re.Pattern[str]:
    """Compile a simple path template into a regex pattern."""
    pattern = re.sub(r"{([^}]+)}", r"(?P<\1>[^/]+)", template.rstrip("/"))
    pattern = f"^{pattern}/?$"
    return re.compile(pattern)


def _normalize_path(path: str) -> str:
    """Ensure the path has a leading slash."""
    if not path.startswith("/"):
        path = "/" + path
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

    def __init__(self, *, name: str | None = None) -> None:
        self._routes: list[
            tuple[str, re.Pattern[str], typing.Callable[..., WebSocketResource]]
        ] = []
        self._names: dict[str, str] = {}
        self.name = name

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
        if any(existing == canonical for existing, _p, _f in self._routes):
            msg = f"route path {path!r} already registered"
            raise ValueError(msg)
        if name and name in self._names:
            msg = f"route name {name!r} already registered"
            raise ValueError(msg)

        factory = functools.partial(resource, *args, **kwargs)

        self._routes.append((path, _compile_template(canonical), factory))
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
        prefix = getattr(req, "path_template", "").rstrip("/")
        if prefix and not req.path.startswith(prefix):
            msg = (
                f"path_template '{prefix}' is not a prefix of request path '{req.path}'"
            )
            raise falcon.HTTPNotFound(description=msg)
        subpath = req.path[len(prefix) :] if prefix else req.path
        subpath = subpath or "/"

        # Routes are tested in the order they were added. Register more
        # specific paths before general ones to control precedence.
        for _template, pattern, factory in self._routes:
            match = pattern.fullmatch(subpath)
            if match:
                try:
                    resource = factory()
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
