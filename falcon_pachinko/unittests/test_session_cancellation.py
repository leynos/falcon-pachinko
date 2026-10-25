"""Focused router-owned session cancellation contracts."""

from __future__ import annotations

import asyncio
import logging
import typing as typ

import falcon
import pytest
from hypothesis import given
from hypothesis import strategies as st

from falcon_pachinko.unittests._session_fixtures import (
    RecordingResource,
    SessionSocket,
    make_session_request,
    make_session_router,
)

_ASSERTION_FAILURE = "lifecycle expectation failed"


@pytest.mark.asyncio
async def test_cancellation_during_receive_closes_and_runs_cleanup(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Cancellation uses abnormal-close metadata without a close frame."""
    caplog.set_level(logging.DEBUG, logger="falcon_pachinko._session_driver")
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

    assert not socket.close_codes, _ASSERTION_FAILURE
    assert RecordingResource.instances[0].disconnect_codes == [1006], _ASSERTION_FAILURE
    observations = [
        record
        for record in caplog.records
        if record.name == "falcon_pachinko._session_driver"
    ]
    assert getattr(observations[-1], "outcome", None) == "cancelled", _ASSERTION_FAILURE
    assert getattr(observations[-1], "close_code", None) == 1006, _ASSERTION_FAILURE


@pytest.mark.asyncio
async def test_cancellation_reports_cleanup_failure_to_loop_handler() -> None:
    """Cleanup failures are observable without replacing cancellation."""
    cleanup = RuntimeError("CANARY-cleanup-secret")
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
    assert reported[0]["operation"] == "cleanup", _ASSERTION_FAILURE
    assert reported[0]["phase"] == "closed", _ASSERTION_FAILURE
    assert reported[0]["close_code"] == 1006, _ASSERTION_FAILURE
    assert reported[0]["exception_type"] == "RuntimeError", _ASSERTION_FAILURE
    assert "CANARY" not in typ.cast("str", reported[0]["message"]), (
        "loop-handler context must omit exception values"
    )


@given(repetitions=st.integers(min_value=1, max_value=4))
def test_generated_repeated_cancellation_waits_for_same_cleanup_task(
    repetitions: int,
) -> None:
    """Generated repeated cancellations cannot interrupt shielded cleanup."""
    asyncio.run(_exercise_repeated_cancellation(repetitions))


async def _exercise_repeated_cancellation(repetitions: int) -> None:
    """Hold disconnect cleanup while delivering a bounded cancellation burst."""
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
    for _ in range(repetitions):
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done(), _ASSERTION_FAILURE

    resource.finish_disconnect.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert resource.disconnect_codes == [1006], _ASSERTION_FAILURE


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
