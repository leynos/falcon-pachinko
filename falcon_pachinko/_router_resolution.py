"""Nested route resolution and resource construction for WebSocketRouter."""

from __future__ import annotations

import typing as typ

from ._route_support import _normalize_path_remaining, _RouteMatch
from ._router_resources import _ResourceResolver

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
    def _validate_and_normalize_path(
        route: _RoutePatterns, req: _RequestPath
    ) -> _RouteMatch | None:
        """Return the path match for ``req`` or ``None`` if invalid."""
        if not (prefix_match := route.prefix.match(req.path)):
            return None
        remaining = req.path[prefix_match.end() :]
        if remaining and not remaining.startswith("/"):
            normalized = _normalize_path_remaining(remaining, prefix_match)
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
        return await _ResourceResolver(self._resource_factory).resolve(
            resource, match, chain, ws
        )
