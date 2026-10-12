"""Nested route resolution and resource construction for WebSocketRouter."""

from __future__ import annotations

import functools
import typing as typ

from ._router_lifecycle import _RouteMatch

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    import re

    from .protocols import WebSocketLike
    from .resource import WebSocketResource


class _RequestPath(typ.Protocol):
    """Request surface consumed during route resolution."""

    @property
    def path(self) -> str:
        """The request path."""


class _RoutePatterns(typ.Protocol):
    """Compiled patterns needed to resolve one route."""

    prefix: re.Pattern[str]
    pattern: re.Pattern[str]


class _RouterResolutionMixin:
    """Resolve nested resources and construct resources for one connection."""

    _resource_factory: cabc.Callable[
        [cabc.Callable[..., WebSocketResource]], WebSocketResource
    ]

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
                new_resource = self._resource_factory(child_factory)
                state_mapping = context.get("state", resource.state)
                new_resource.state = typ.cast(
                    "cabc.MutableMapping[str, typ.Any]",
                    state_mapping,
                )
                return new_resource, remaining, match.groupdict()
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
        self, route: _RoutePatterns, req: _RequestPath
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
