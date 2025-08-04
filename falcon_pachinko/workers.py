"""Utilities for managing background asyncio workers."""

from __future__ import annotations

import asyncio
import collections.abc as cabc
from contextlib import AsyncExitStack

WorkerFn = cabc.Callable[..., cabc.Awaitable[None]]


class WorkerController:
    """Manage a set of long-running tasks bound to an ASGI lifespan."""

    __slots__ = ("_stack", "_tasks")

    def __init__(self) -> None:
        self._tasks: list[asyncio.Task[None]] = []
        self._stack: AsyncExitStack | None = None

    async def start(self, *workers: WorkerFn, **context: object) -> None:
        """Schedule *workers* as tasks, injecting shared *context*."""
        self._stack = AsyncExitStack()
        await self._stack.__aenter__()

        for fn in workers:
            task = asyncio.create_task(fn(**context))
            self._tasks.append(task)

    async def stop(self) -> None:
        """Cancel worker tasks and propagate the first exception, if any."""
        error: Exception | None = None
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        for t in self._tasks:
            try:
                exc = t.exception()
            except asyncio.CancelledError:
                continue
            if exc and error is None:
                error = exc
        if self._stack:
            await self._stack.__aexit__(None, None, None)
        if error:
            raise error


def worker(fn: WorkerFn) -> WorkerFn:
    """Mark *fn* as a background worker."""
    fn.__pachinko_worker__ = True  # type: ignore[attr-defined]
    return fn
