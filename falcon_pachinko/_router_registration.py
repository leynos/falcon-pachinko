"""Route path normalization and registration conflict checks."""

from __future__ import annotations

import typing as typ

if typ.TYPE_CHECKING:
    import threading


class _RouteDefinition(typ.Protocol):
    """Route field used to detect duplicate registrations."""

    canonical: str


class _RouteRegistrationService[RouteT: _RouteDefinition]:
    """Manage route normalization and conflict detection for a router."""

    def __init__(
        self,
        *,
        lock: threading.Lock,
        raw_routes: list[RouteT],
        names: dict[str, str],
    ) -> None:
        self._lock = lock
        self._raw_routes = raw_routes
        self._names = names

    def check_conflicts(
        self, canonical: str, name: str | None, *, path: str | None = None
    ) -> None:
        """Raise if ``canonical`` or ``name`` already exists under ``lock``.

        Raises
        ------
        RuntimeError
            If the mount lock is not held by the caller.
        ValueError
            If the canonical path or the route name is already registered.
        """
        if not self._lock.locked():
            msg = (
                "_RouteRegistrationService.check_conflicts requires "
                "_mount_lock to be held"
            )
            raise RuntimeError(msg)

        display_path = path if path is not None else canonical
        if any(route.canonical == canonical for route in self._raw_routes):
            msg = f"route path {display_path!r} already registered"
            raise ValueError(msg)
        if name and name in self._names:
            msg = f"route name {name!r} already registered"
            raise ValueError(msg)
