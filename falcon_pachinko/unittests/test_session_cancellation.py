"""Focused router-owned session cancellation contracts."""

from __future__ import annotations

import asyncio
import typing as typ

import falcon
import pytest

from falcon_pachinko.unittests._session_fixtures import (
    RecordingResource,
    SessionSocket,
    make_session_request,
    make_session_router,
)

_ASSERTION_FAILURE = "lifecycle expectation failed"


@pytest.mark.asyncio
async def test_cancellation_during_receive_closes_and_runs_cleanup() -> None:
    """Cancellation closes with 1001 and is re-raised after cleanup."""
    router = make_session_router()
    socket = SessionSocket(block_receive=True)
    task = asyncio.create_task(
        router.on_websocket(
            typ.cast("typ.Any", make_session_request()), typ.cast("typ.Any", socket)
        )
    )
    await socket.receive_started.wait()

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert socket.close_codes == [1001], _ASSERTION_FAILURE
    assert RecordingResource.instances[0].disconnect_codes == [1001], _ASSERTION_FAILURE


@pytest.mark.asyncio
async def test_cancellation_reports_cleanup_failure_to_loop_handler() -> None:
    """Cleanup failures are observable without replacing cancellation."""
    cleanup = RuntimeError("disconnect failed")
    loop = asyncio.get_running_loop()
    reported: list[dict[str, object]] = []
    old_handler = loop.get_exception_handler()
    loop.set_exception_handler(lambda _loop, context: reported.append(context))

    class FailingCleanupResource(RecordingResource):
        async def on_disconnect(self, ws: object, close_code: int) -> None:
            await super().on_disconnect(ws, close_code)
            raise cleanup

    router = make_session_router(FailingCleanupResource)
    socket = SessionSocket(block_receive=True)
    task = asyncio.create_task(
        router.on_websocket(
            typ.cast("typ.Any", make_session_request()), typ.cast("typ.Any", socket)
        )
    )
    try:
        await socket.receive_started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        loop.set_exception_handler(old_handler)

    assert len(reported) == 1, _ASSERTION_FAILURE
    assert reported[0]["exception"] is cleanup, _ASSERTION_FAILURE
    assert "cleanup failed" in typ.cast("str", reported[0]["message"]), (
        _ASSERTION_FAILURE
    )


@pytest.mark.asyncio
async def test_repeated_cancellation_waits_for_same_cleanup_task() -> None:
    """Repeated cancellation cannot interrupt the shielded disconnect hook."""
    router = make_session_router()
    socket = SessionSocket(block_receive=True)
    task = asyncio.create_task(
        router.on_websocket(
            typ.cast("typ.Any", make_session_request()), typ.cast("typ.Any", socket)
        )
    )
    await socket.receive_started.wait()
    resource = RecordingResource.instances[0]
    resource.wait_in_disconnect = True

    task.cancel()
    await resource.disconnect_started.wait()
    task.cancel()
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done(), _ASSERTION_FAILURE

    resource.finish_disconnect.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert resource.disconnect_codes == [1001], _ASSERTION_FAILURE


@pytest.mark.asyncio
async def test_cancellation_during_clean_disconnect_preserves_selected_code() -> None:
    """Late cancellation finishes cleanup without rewriting the close outcome."""

    class WaitingCleanupResource(RecordingResource):
        def __init__(self) -> None:
            super().__init__()
            self.wait_in_disconnect = True

    router = make_session_router(WaitingCleanupResource)
    socket = SessionSocket(falcon.WebSocketDisconnected(4000))
    task = asyncio.create_task(
        router.on_websocket(
            typ.cast("typ.Any", make_session_request()), typ.cast("typ.Any", socket)
        )
    )
    await socket.receive_started.wait()
    resource = WaitingCleanupResource.instances[0]
    await resource.disconnect_started.wait()
    task.cancel()
    resource.finish_disconnect.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert resource.disconnect_codes == [4000], (
        "cancellation during cleanup must preserve the selected close code"
    )
    assert not socket.close_codes, "a completed client close must not be rewritten"
