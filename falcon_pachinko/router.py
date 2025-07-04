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


class WebSocketRouter:
    """Minimal Falcon resource for routing WebSocket connections."""

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

        factory = functools.partial(resource, *args, **kwargs)

        self._routes.append((path, _compile_template(path), factory))
        if name:
            self._names[name] = path

    def url_for(self, name: str, **params: object) -> str:
        """Return the URL path associated with ``name`` formatted with ``params``."""
        template = self._names[name]
        return template.format(**params)

    async def on_websocket(
        self, req: falcon.Request, ws: WebSocketLike
    ) -> None:  # pragma: no cover - simple wrapper
        """Dispatch the connection to the first matching route.

        Assumes that ``req.path_template`` (if present) is a prefix of
        ``req.path``.
        """
        prefix = getattr(req, "path_template", "").rstrip("/")
        if prefix and not req.path.startswith(prefix):
            msg = (
                f"path_template '{prefix}' is not a prefix of request path '{req.path}'"
            )
            raise ValueError(msg)
        subpath = req.path[len(prefix) :] if prefix else req.path
        subpath = subpath or "/"

        for _template, pattern, factory in self._routes:
            match = pattern.fullmatch(subpath)
            if match:
                resource = factory()
                should_accept = await resource.on_connect(req, ws, **match.groupdict())
                if not should_accept:
                    await ws.close()
                    return
                await ws.accept()
                return

        raise falcon.HTTPNotFound
