"""Minimal Falcon ASGI app with a lifespan decorator for tests and examples."""

from __future__ import annotations

import contextlib as cl
import typing as t

import falcon.asgi

if t.TYPE_CHECKING:  # pragma: no cover - typing helpers
    import collections.abc as cabc
    import contextlib as cl_typing


class LifespanApp(falcon.asgi.App):
    """Falcon ASGI App with a minimal lifespan decorator."""

    def __init__(self) -> None:
        super().__init__()
        self._lifespan_handler: (
            t.Callable[[LifespanApp], cl_typing.AbstractAsyncContextManager[None]]
            | None
        ) = None

    def lifespan(
        self, fn: t.Callable[[LifespanApp], cabc.AsyncIterator[None]]
    ) -> t.Callable[[LifespanApp], cl_typing.AbstractAsyncContextManager[None]]:  # type: ignore[override]
        """Register a lifespan context manager."""
        manager = cl.asynccontextmanager(fn)
        self._lifespan_handler = manager
        return manager

    def lifespan_context(self) -> cl_typing.AbstractAsyncContextManager[None]:
        """Return the registered lifespan context manager."""
        if self._lifespan_handler is None:
            msg = "lifespan handler not set"
            raise RuntimeError(msg)
        return self._lifespan_handler(self)
