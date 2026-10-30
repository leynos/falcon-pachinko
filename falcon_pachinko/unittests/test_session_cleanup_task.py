"""Preserve failure precedence when disconnect housekeeping raises unexpectedly."""

from __future__ import annotations

import asyncio
import dataclasses as dc
import typing as typ

import pytest

from falcon_pachinko._session import SessionState
from falcon_pachinko._session_cleanup import (
    CleanupContext,
    raise_session_failures,
    run_shielded_cleanup,
)
from falcon_pachinko.hooks import HookCollection, HookManager
from falcon_pachinko.unittests._session_fixtures import (
    RecordingResource,
    SessionSocket,
    make_session_request,
)

if typ.TYPE_CHECKING:
    import falcon

    from falcon_pachinko.protocols import WebSocketLike


class _FailingClosedState(SessionState):
    """Raise after hooks finish to exercise an unexpected cleanup task failure."""

    def __init__(self, failure: RuntimeError) -> None:
        """Establish a session whose final state transition raises the test error."""
        super().__init__(close_code=4000)
        self.failure = failure
        self.establish()

    def mark_closed(self) -> None:
        """Complete the state transition before simulating housekeeping failure."""
        super().mark_closed()
        raise self.failure


@dc.dataclass(slots=True)
class _CleanupDispatch:
    """Provide the request and socket fields consumed by disconnect cleanup."""

    req: falcon.Request
    ws: WebSocketLike


def _cleanup_context(
    failure: RuntimeError, primary_error: Exception | None = None
) -> CleanupContext:
    """Assemble the real cleanup path with one injected housekeeping failure."""
    resource = RecordingResource()
    # The lightweight request supplies the fields exercised by these tests.
    dispatch = _CleanupDispatch(
        typ.cast("falcon.Request", make_session_request()), SessionSocket()
    )
    return CleanupContext(
        resource=resource,
        dispatch=dispatch,
        hook_manager=HookManager(global_hooks=HookCollection(), resources=[resource]),
        session=_FailingClosedState(failure),
        params={},
        primary_error=primary_error,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "primary_error",
    [
        pytest.param(None, id="cleanup-only"),
        pytest.param(ValueError("session failed"), id="primary-first"),
    ],
)
async def test_unexpected_cleanup_task_failure_preserves_precedence(
    primary_error: Exception | None,
) -> None:
    """Collect task-level errors before applying the ordinary failure policy."""
    failure = RuntimeError("cleanup task failed")
    context = _cleanup_context(failure, primary_error)
    failures, interruptions = await run_shielded_cleanup(context)
    assert failures == (failure,), "unexpected cleanup errors must remain available"
    assert not interruptions, "ordinary cleanup has no cancellation"
    reported: list[dict[str, object]] = []
    loop = asyncio.get_running_loop()
    previous_handler = loop.get_exception_handler()
    loop.set_exception_handler(lambda _loop, details: reported.append(details))
    try:
        expected_error = primary_error or failure
        with pytest.raises(type(expected_error)) as raised:
            raise_session_failures(primary_error, failures, interruptions)
    finally:
        loop.set_exception_handler(previous_handler)
    assert raised.value is expected_error, "the selected failure must be preserved"
    if primary_error is not None:
        assert [details["exception"] for details in reported] == [failure], (
            "finalization failure must remain observable without replacing "
            "the session error"
        )
    resource = typ.cast("RecordingResource", context.resource)
    assert len(resource.disconnect_codes) == 1, "hooks must finish before task failure"


@pytest.mark.asyncio
async def test_unexpected_cleanup_task_failure_retains_cancellation() -> None:
    """Keep cancellation bare and report a task failure that occurs afterward."""
    failure = RuntimeError("cleanup task failed after cancellation")
    context = _cleanup_context(failure)
    resource = typ.cast("RecordingResource", context.resource)
    resource.wait_in_disconnect = True
    reported: list[dict[str, object]] = []
    loop = asyncio.get_running_loop()
    previous_handler = loop.get_exception_handler()
    loop.set_exception_handler(lambda _loop, details: reported.append(details))

    async def finish_cleanup() -> None:
        """Apply failure precedence after the shielded cleanup task completes."""
        failures, interruptions = await run_shielded_cleanup(context)
        raise_session_failures(None, failures, interruptions)

    task = asyncio.create_task(finish_cleanup())
    try:
        await asyncio.wait_for(resource.disconnect_started.wait(), timeout=1)
        task.cancel("cancelled session")
        await asyncio.sleep(0)
        resource.finish_disconnect.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=1)
        assert [details["exception"] for details in reported] == [failure], (
            "cancellation must not hide cleanup task errors"
        )
        assert reported[0]["message"] == (
            "WebSocket disconnect cleanup failed during cancellation"
        ), "the session policy must own the failure report"
        assert resource.disconnect_codes == [4000], (
            "cancellation during cleanup must retain the selected code"
        )
    finally:
        resource.finish_disconnect.set()
        await asyncio.gather(task, return_exceptions=True)
        loop.set_exception_handler(previous_handler)
