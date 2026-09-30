"""Utilities for managing background asyncio workers."""

from __future__ import annotations

import asyncio
import collections.abc as cabc
import typing as typ

type WorkerFn = cabc.Callable[..., cabc.Coroutine[object, object, None]]


class WorkerController:
    """Manage long-running tasks bound to an ASGI lifespan.

    The application passes each worker explicitly to :meth:`start`. The
    controller provides no worker discovery or registry.

    Methods
    -------
    start
        Schedule worker tasks and inject shared context.
    stop
        Cancel worker tasks and propagate the first exception, if any.
    """

    __slots__ = ("_tasks",)

    def __init__(self) -> None:
        self._tasks: list[asyncio.Task[None]] = []

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

        If creating or scheduling a worker fails, cancel and await all workers
        already scheduled, reset the controller, and re-raise the original
        error.

        Raises
        ------
        RuntimeError
            If the controller has already been started.
        """
        if self._tasks:
            msg = "WorkerController is already started"
            raise RuntimeError(msg)

        try:
            for fn in workers:
                task = self._schedule_worker(fn, context)
                self._tasks.append(task)
        except BaseException:
            await self._rollback_start()
            raise

    @staticmethod
    def _schedule_worker(
        fn: WorkerFn,
        context: dict[str, object],
    ) -> asyncio.Task[None]:
        """Create one worker task.

        Parameters
        ----------
        fn : WorkerFn
            Factory that returns the worker coroutine.
        context : dict[str, object]
            Keyword arguments shared with the worker.

        Returns
        -------
        asyncio.Task[None]
            The task running the worker.
        """
        coroutine = fn(**context)
        try:
            return asyncio.create_task(coroutine)
        except BaseException:
            if isinstance(coroutine, cabc.Coroutine):
                # Closing a rejected coroutine prevents a never-awaited leak.
                coroutine.close()
            raise

    async def _rollback_start(self) -> None:
        """Cancel partial startup and restore the controller to a fresh state."""
        try:
            self._cancel_all_tasks()
            await self._wait_for_tasks()
        finally:
            self._tasks.clear()

    async def stop(self) -> None:
        """Cancel worker tasks and propagate the first exception, if any."""
        self._cancel_all_tasks()
        await self._wait_for_tasks()
        error = self._collect_first_exception()
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
