"""Path-template and registration support for the WebSocket router."""

from __future__ import annotations

import dataclasses as dc
import re
import typing as typ

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    import threading

    from .resource import WebSocketResource


class _RequestLike(typ.Protocol):
    """Request surface consumed by the router before resource dispatch."""

    @property
    def path(self) -> str:
        """The request path."""


@dc.dataclass(slots=True)
class _RawRoute:
    """A registered route before its mount prefix is known."""

    template: str
    canonical: str
    factory: cabc.Callable[..., WebSocketResource]


@dc.dataclass(slots=True)
class _CompiledRoute:
    """A route compiled with its mount prefix."""

    prefix: re.Pattern[str]
    pattern: re.Pattern[str]
    factory: cabc.Callable[..., WebSocketResource]


@dc.dataclass(slots=True)
class _RouteMatch:
    """Captured route parameters plus the path segment still to be consumed."""

    params: dict[str, str]
    remaining: str


def _request_path_template(req: _RequestLike) -> str:
    """Return Falcon's selected route template, with a legacy fallback."""
    template = getattr(req, "uri_template", None)
    if not template:
        template = getattr(req, "path_template", "")

    # Falcon reports the descendant route template, so compare its mount
    # identity rather than the request-specific catch-all segment.
    match = re.fullmatch(r"(?P<prefix>.*)/\{[A-Za-z_]\w*:path\}", template)
    return match.group("prefix") or "/" if match is not None else template


def _normalize_path(path: str) -> str:
    """Ensure the path has a leading slash."""
    return path if path.startswith("/") else f"/{path}"


def _canonical_path(path: str) -> str:
    """Return the normalized path without a trailing slash."""
    path = _normalize_path(path)
    return path if path == "/" else path.rstrip("/")


class _RouteRegistrationService:
    """Manage route normalization and conflict detection for a router."""

    def __init__(
        self,
        *,
        lock: threading.Lock,
        raw_routes: list[_RawRoute],
        names: dict[str, str],
    ) -> None:
        """Retain the router registries and lock used for registration."""
        self._lock = lock
        self._raw_routes = raw_routes
        self._names = names

    @staticmethod
    def normalize_path(path: str) -> tuple[str, str]:
        """Return normalized and canonical variants of ``path``."""
        normalized = _normalize_path(path)
        return normalized, _canonical_path(normalized)

    def check_conflicts(
        self, canonical: str, name: str | None, *, path: str | None = None
    ) -> None:
        """Raise if ``canonical`` or ``name`` already exists under ``lock``."""
        self._ensure_lock_held()
        display_path = path if path is not None else canonical
        self._ensure_path_available(canonical, display_path)
        self._ensure_name_available(name)

    def _ensure_lock_held(self) -> None:
        """Reject conflict checks that run outside the router lock."""
        if not self._lock.locked():
            msg = (
                "_RouteRegistrationService.check_conflicts requires "
                "_mount_lock to be held"
            )
            raise RuntimeError(msg)

    def _ensure_path_available(self, canonical: str, display_path: str) -> None:
        """Reject a canonical path already present in the route registry."""
        if any(route.canonical == canonical for route in self._raw_routes):
            msg = f"route path {display_path!r} already registered"
            raise ValueError(msg)

    def _ensure_name_available(self, name: str | None) -> None:
        """Reject a route name already present in the name registry."""
        if name and name in self._names:
            msg = f"route name {name!r} already registered"
            raise ValueError(msg)
