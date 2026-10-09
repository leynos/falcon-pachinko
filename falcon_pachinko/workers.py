"""Utilities for managing background asyncio workers."""

from __future__ import annotations

import asyncio
import collections.abc as cabc
import typing as typ

type WorkerFn = cabc.Callable[..., cabc.Coroutine[object, object, None]]
_RollbackResult = BaseException | None
_FutureResult = typ.TypeVar("_FutureResult")


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
        except BaseException as startup_error:
            await self._rollback_start_preserving_error(startup_error)
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

    async def _rollback_start(self) -> _RollbackResult:
        """Cancel partial startup and restore the controller to a fresh state.

        Continue awaiting cleanup if the caller is cancelled during rollback.

        Returns
        -------
        BaseException | None
            The first rollback failure or cancellation while waiting, if any.
        """
        rollback_task = asyncio.ensure_future(self._finish_startup_rollback())
        cancellation_error = await self._await_rollback_task(rollback_task)
        if rollback_task.cancelled():
            rollback_error: BaseException | None = asyncio.CancelledError()
        else:
            rollback_error = rollback_task.exception()
        if rollback_error is not None:
            if cancellation_error is not None:
                rollback_error.add_note(
                    f"Rollback waiting was also cancelled: {cancellation_error!r}"
                )
            return rollback_error
        return cancellation_error

    async def _rollback_start_preserving_error(
        self,
        startup_error: BaseException,
    ) -> None:
        """Record rollback failures without replacing the startup error."""
        rollback_task = asyncio.ensure_future(self._rollback_start())
        cancellation_error = await self._await_rollback_task(rollback_task)
        if rollback_task.cancelled():
            rollback_result: _RollbackResult = asyncio.CancelledError()
        else:
            rollback_result = rollback_task.exception()
            if rollback_result is None:
                rollback_result = rollback_task.result()
        if cancellation_error is not None:
            startup_error.add_note(
                f"Worker startup rollback was also cancelled: {cancellation_error!r}"
            )
        if rollback_result is not None:
            startup_error.add_note(
                f"Worker startup rollback raised {rollback_result!r}"
            )

    @staticmethod
    async def _await_rollback_task(
        rollback_task: asyncio.Future[_FutureResult],
    ) -> asyncio.CancelledError | None:
        """Wait for rollback completion and retain caller cancellation."""
        cancellation_error: asyncio.CancelledError | None = None

        while not rollback_task.done():
            try:
                await asyncio.wait({rollback_task})
            except asyncio.CancelledError as error:
                cancellation_error = cancellation_error or error
        return cancellation_error

    async def _finish_startup_rollback(self) -> None:
        """Attempt each rollback step and reset controller state."""
        rollback_errors: list[BaseException] = []
        try:
            self._cancel_all_tasks()
            wait_results = await asyncio.gather(
                self._wait_for_tasks(),
                return_exceptions=True,
            )
            rollback_errors.extend(
                error for error in wait_results if isinstance(error, BaseException)
            )
        finally:
            self._tasks.clear()

        if rollback_errors:
            primary_error = rollback_errors[0]
            for secondary_error in rollback_errors[1:]:
                primary_error.add_note(
                    f"Additional worker startup rollback failure: {secondary_error!r}"
                )
            raise primary_error

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
