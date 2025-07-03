"""Composable WebSocket routing utilities."""

from __future__ import annotations

import dataclasses as dc
import typing

import falcon.routing.compiled as compiled

if typing.TYPE_CHECKING:
    import falcon

    from .resource import WebSocketLike, WebSocketResource


@dc.dataclass(slots=True)
class _Route:
    path: str
    target: typing.Callable[..., WebSocketResource] | type[WebSocketResource]
    args: tuple[typing.Any, ...]
    kwargs: dict[str, typing.Any]
    name: str | None


class WebSocketRouteNotFoundError(LookupError):
    """Raised when no matching WebSocket route exists."""


class DuplicateRouteNameError(ValueError):
    """Raised when attempting to reuse a route name."""


class WebSocketRouter:
    """Route WebSocket connections relative to its mount point."""

    def __init__(self, *, name: str | None = None) -> None:
        self.name = name
        self._mount_path: str | None = None
        self._router = compiled.CompiledRouter()
        self._routes: list[_Route] = []
        self._name_map: dict[str, _Route] = {}

    def add_route(
        self,
        path: str,
        target: typing.Callable[..., WebSocketResource] | type[WebSocketResource],
        *,
        name: str | None = None,
        init_args: tuple[typing.Any, ...] | None = None,
        init_kwargs: dict[str, typing.Any] | None = None,
    ) -> None:
        """Register ``target`` to handle connections for ``path``."""
        if name and name in self._name_map:
            raise DuplicateRouteNameError(name)

        route = _Route(
            path,
            target,
            init_args or (),
            init_kwargs or {},
            name,
        )
        self._router.add_route(path, route)
        self._routes.append(route)
        if name:
            self._name_map[name] = route

    def url_for(self, name: str, **params: str) -> str:
        """Return the relative URI for the named route."""
        try:
            route = self._name_map[name]
        except KeyError:  # pragma: no cover - defensive
            raise ValueError(f"No route found with name: {name!r}") from None  # noqa: TRY003

        return route.path.format(**params)

    async def on_websocket(
        self, req: falcon.Request, ws: WebSocketLike
    ) -> WebSocketResource:
        """Handle a WebSocket connection dispatched to this router."""
        if self._mount_path is None:
            self._mount_path = req.uri_template
        path = req.path[len(self._mount_path) :] or "/"
        match = self._router.find(path)
        if not match:
            raise WebSocketRouteNotFoundError(path)
        route = typing.cast("_Route", match[0])
        params = match[2]
        target = route.target
        resource: WebSocketResource = target(*route.args, **route.kwargs)
        await resource.on_connect(req, ws, **params)
        return resource
