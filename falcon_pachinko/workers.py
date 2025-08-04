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
        """Schedule *workers* as tasks, injecting shared *context*.
        Raises ``RuntimeError`` if already started.
        """
        if self._tasks:
            msg = "WorkerController is already started"
            raise RuntimeError(msg)

        self._stack = AsyncExitStack()
        await self._stack.__aenter__()

        for fn in workers:
            task = asyncio.create_task(fn(**context))
            self._tasks.append(task)

    async def stop(self) -> None:
        """Cancel worker tasks and propagate the first exception, if any.
        The controller may be stopped multiple times; subsequent calls will
        return immediately.
        """
        if not self._tasks:
            return

        self._cancel_all_tasks()
        await self._wait_for_tasks()
        error = self._collect_first_exception()
        await self._cleanup_stack()
        self._tasks.clear()
        if error:
            raise error

    def _cancel_all_tasks(self) -> None:
        """Request cancellation for all running worker tasks."""
        for task in self._tasks:
            task.cancel()

    async def _wait_for_tasks(self) -> None:
        """Wait for all worker tasks to finish after cancellation."""
        await asyncio.gather(*self._tasks, return_exceptions=True)

    def _collect_first_exception(self) -> Exception | None:
        """Return the first non-cancellation exception from workers, if any."""
        for task in self._tasks:
            try:
                exc = task.exception()
            except asyncio.CancelledError:
                continue
            if exc:
                return exc
        return None

    async def _cleanup_stack(self) -> None:
        """Exit the internal AsyncExitStack, if any."""
        if self._stack:
            await self._stack.__aexit__(None, None, None)
            self._stack = None


def worker(fn: WorkerFn) -> WorkerFn:
    """Mark *fn* as a background worker."""
    fn.__pachinko_worker__ = True  # type: ignore[attr-defined]
    return fn
