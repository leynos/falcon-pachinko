"""Shared helpers for constructing router-level resource factories in tests."""

from __future__ import annotations

import typing as typ

if typ.TYPE_CHECKING:
    from falcon_pachinko import WebSocketResource
    from falcon_pachinko.router import ResourceFactory


def resource_factory(service: object) -> ResourceFactory:
    """Return a factory injecting ``service`` into created resources."""

    def build(
        route_factory: typ.Callable[..., WebSocketResource],
    ) -> WebSocketResource:
        target = getattr(route_factory, "func", route_factory)
        args = getattr(route_factory, "args", ())
        base_kwargs = dict(getattr(route_factory, "keywords", {}) or {})
        base_kwargs["service"] = service
        return target(*args, **base_kwargs)

    return build
