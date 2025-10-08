"""Dependency injection helpers for WebSocket resource factories."""

from __future__ import annotations

import inspect
import typing as typ

if typ.TYPE_CHECKING:  # pragma: no cover - used only for static analysis
    from .resource import WebSocketResource

    WebSocketResourceT = typ.TypeVar("WebSocketResourceT", bound=WebSocketResource)
else:  # pragma: no cover - runtime fallback for annotations
    WebSocketResourceT = typ.TypeVar("WebSocketResourceT")

__all__ = ["ServiceContainer", "ServiceNotFoundError"]


class ServiceNotFoundError(LookupError):
    """Raised when a requested dependency is not registered."""

    name: str

    def __init__(self, name: str) -> None:
        self.name = name
        super().__init__(f"service {name!r} is not registered")


class ServiceContainer:
    """Minimal container used to demonstrate router-level DI wiring."""

    def __init__(self) -> None:
        self._services: dict[str, object] = {}
        self._signature_cache: dict[typ.Callable[..., object], inspect.Signature] = {}

    def register(self, name: str, value: object) -> None:
        """Expose ``value`` for resources requesting ``name``."""
        self._services[name] = value

    def resolve(self, name: str) -> object:
        """Return the registered dependency named ``name``."""
        try:
            return self._services[name]
        except KeyError as exc:  # pragma: no cover - used interactively
            raise ServiceNotFoundError(name) from exc

    def create_resource(
        self, route_factory: typ.Callable[..., WebSocketResourceT]
    ) -> WebSocketResourceT:
        """Instantiate ``route_factory`` injecting registered dependencies."""
        target = typ.cast(
            "typ.Callable[..., WebSocketResourceT]",
            getattr(route_factory, "func", route_factory),
        )
        args = getattr(route_factory, "args", ())
        kwargs = dict(getattr(route_factory, "keywords", {}) or {})

        signature = self._signature_cache.get(target)
        if signature is None:
            signature = inspect.signature(target)
            self._signature_cache[target] = signature

        for parameter in signature.parameters.values():
            if parameter.name == "self":
                continue
            if parameter.kind in (
                inspect.Parameter.VAR_POSITIONAL,
                inspect.Parameter.VAR_KEYWORD,
            ):
                continue
            if parameter.name in kwargs:
                continue
            if parameter.name in self._services:
                kwargs[parameter.name] = self._services[parameter.name]

        return target(*args, **kwargs)
