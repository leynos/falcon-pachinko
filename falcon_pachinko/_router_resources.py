"""Nested resource traversal and construction for the WebSocket router."""

from __future__ import annotations

import functools
import typing as typ

from ._route_support import _normalize_path_remaining, _RouteMatch

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from .protocols import WebSocketLike
    from .resource import WebSocketResource


class _ResourceResolver:
    """Instantiate a route resource and consume its nested subroutes."""

    def __init__(
        self,
        resource_factory: cabc.Callable[
            [cabc.Callable[..., WebSocketResource]], WebSocketResource
        ],
    ) -> None:
        """Retain the factory used for each resource in a route chain."""
        self._resource_factory = resource_factory

    async def instantiate(
        self,
        route_factory: cabc.Callable[..., WebSocketResource],
        ws: WebSocketLike,
    ) -> WebSocketResource:
        """Instantiate a resource and mark failures after closing the socket."""
        try:
            return self._resource_factory(route_factory)
        except Exception as exc:  # pragma: no cover - exercised by router tests
            await ws.close()
            # The marker prevents the router from attempting a second close.
            typ.cast("typ.Any", exc)._pachinko_factory_closed = True
            raise

    async def resolve(
        self,
        resource: WebSocketResource,
        match: _RouteMatch,
        chain: list[WebSocketResource],
        ws: WebSocketLike,
    ) -> WebSocketResource | None:
        """Return the final resource when the nested path is fully consumed."""
        resolved = await self._resolve_subroutes(resource, match, chain, ws)
        return resolved if match.remaining in {"", "/"} else None

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

    async def _try_subroute_match(
        self,
        resource: WebSocketResource,
        path: str,
        ws: WebSocketLike,
    ) -> tuple[WebSocketResource, str, dict[str, str]] | None:
        """Instantiate the first child resource whose template matches."""
        for pattern, factory in getattr(resource, "_subroutes", []):
            if match := pattern.match(path):
                remaining = path[match.end() :]
                if (remaining := _normalize_path_remaining(remaining, match)) is None:
                    return None
                context = resource.get_child_context()
                child_kwargs = {
                    key: value for key, value in context.items() if key != "state"
                }
                child_factory = functools.partial(factory, **child_kwargs)
                new_resource = await self.instantiate(child_factory, ws)
                state_mapping = context.get("state", resource.state)
                # The setter validates the mapping at runtime; the context is
                # intentionally exposed as object to child resource factories.
                new_resource.state = typ.cast(
                    "cabc.MutableMapping[str, typ.Any]",
                    state_mapping,
                )
                return new_resource, remaining, match.groupdict()
        return None
