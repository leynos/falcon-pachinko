"""Utilities for managing background asyncio workers."""

from __future__ import annotations

import asyncio
import collections.abc as cabc
import typing as typ
from contextlib import AsyncExitStack

type WorkerFn = cabc.Callable[..., cabc.Coroutine[object, object, None]]


class WorkerController:
    """Manage long-running tasks bound to an ASGI lifespan.

    The application passes each worker explicitly to :meth:`start`. The
    controller provides no worker discovery or registry.
    """

    __slots__ = ("_stack", "_tasks")

    def __init__(self) -> None:
        self._tasks: list[asyncio.Task[None]] = []
        self._stack: AsyncExitStack | None = None

    async def start(self, *workers: WorkerFn, **context: object) -> None:
        """Schedule *workers* as tasks, injecting shared *context*.

        *workers* may be any async callable that accepts the supplied keyword
        arguments, with or without the :func:`worker` decorator. Every keyword
        argument in *context* is passed to every worker.

        Parameters
        ----------
        *workers : WorkerFn
            Async callables to schedule. Each must accept every keyword
            argument in *context*.
        **context : object
            Keyword arguments passed to every worker.

        Raises
        ------
        RuntimeError
            If the controller has already been started.
        """
        if self._tasks:
            msg = "WorkerController is already started"
            raise RuntimeError(msg)

        # A freshly constructed AsyncExitStack needs no explicit entry; entering
        # it is a no-op that merely returns the stack itself.
        self._stack = AsyncExitStack()

        for fn in workers:
            coroutine = fn(**context)
            task = asyncio.create_task(coroutine)
            self._tasks.append(task)

    async def stop(self) -> None:
        """Cancel worker tasks and propagate the first exception, if any."""
        self._cancel_all_tasks()
        await self._wait_for_tasks()
        error = self._collect_first_exception()
        await self._cleanup_stack()
        self._tasks.clear()
        if error:
            raise error

    def _cancel_all_tasks(self) -> None:
        """Cancel all running worker tasks."""
        for task in self._tasks:
            task.cancel()

    async def _wait_for_tasks(self) -> None:
        """Wait for all tasks to complete, ignoring exceptions."""
        await asyncio.gather(*self._tasks, return_exceptions=True)

    def _collect_first_exception(self) -> Exception | None:
        """Collect the first non-cancellation exception from completed tasks."""
        for task in self._tasks:
            try:
                exc = task.exception()
            except asyncio.CancelledError:
                continue
            if isinstance(exc, Exception):
                return exc
        return None

    async def _cleanup_stack(self) -> None:
        """Clean up the async context stack."""
        if self._stack:
            await self._stack.aclose()


def worker(fn: WorkerFn) -> WorkerFn:
    """Mark *fn* as a background worker.

    Set ``__pachinko_worker__ = True`` on *fn* for readers and introspection
    tools. Return the same function object without wrapping or validating it.
    Decoration is optional, and :meth:`WorkerController.start` never reads
    this marker.

    Parameters
    ----------
    fn : WorkerFn
        Async callable to mark.

    Returns
    -------
    WorkerFn
        The same function object passed as *fn*.
    """
    # cast: ``WorkerFn`` is a bare callable alias, so the checker cannot see
    # the dynamic marker attribute stamped onto the function object.
    typ.cast("typ.Any", fn).__pachinko_worker__ = True
    return fn
