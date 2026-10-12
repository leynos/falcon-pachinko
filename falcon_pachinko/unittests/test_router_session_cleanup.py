"""Exactly-once router cleanup for cancelled or failed handshakes."""

from __future__ import annotations

import asyncio
import typing as typ

import pytest

from falcon_pachinko.unittests.helpers import make_req
from falcon_pachinko.unittests.router_session_helpers import (
    SessionResource,
    SessionSocket,
    session_router,
)

if typ.TYPE_CHECKING:
    from falcon_pachinko import HookContext


@pytest.mark.asyncio
async def test_receive_loop_cancellation_disconnects_once() -> None:
    """Cancellation cleans up accepted sessions and remains observable."""
    resource = SessionResource()
    socket = SessionSocket(None)
    router = session_router(resource, receive_loop=True)
    task = asyncio.create_task(router.on_websocket(make_req("/session"), socket))
    try:
        async with asyncio.timeout(1):
            await socket.receiving.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert resource.disconnects == [(socket, 1006)], (
            "cancellation must trigger exactly one cleanup"
        )
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_receive_loop_acceptance_failure_disconnects_once() -> None:
    """A successful on_connect is cleaned up even when accept fails."""
    resource = SessionResource()
    socket = SessionSocket(accept_error=RuntimeError("accept failed"))
    router = session_router(resource, receive_loop=True)
    with pytest.raises(RuntimeError, match="accept failed"):
        await router.on_websocket(make_req("/session"), socket)
    assert resource.disconnects == [(socket, 1006)], (
        "accept failure must clean up registered state once"
    )
    assert socket.closed, "accept failure must use the existing error close path"


@pytest.mark.asyncio
async def test_receive_loop_after_connect_failure_disconnects_once() -> None:
    """Post-connect hook errors release state acquired by on_connect."""
    resource = SessionResource()
    socket = SessionSocket()
    router = session_router(resource, receive_loop=True)

    def fail_after_connect(context: HookContext) -> None:
        msg = "after_connect failed"
        raise RuntimeError(msg)

    router.global_hooks.add("after_connect", fail_after_connect)
    with pytest.raises(RuntimeError, match="after_connect failed"):
        await router.on_websocket(make_req("/session"), socket)
    assert resource.disconnects == [(socket, 1006)], (
        "hook failure must clean up registered state once"
    )
    assert socket.closed, "hook failure must use the existing error close path"


@pytest.mark.asyncio
async def test_receive_loop_runs_disconnect_hooks_before_cleanup() -> None:
    """Router-owned sessions retain the public before-disconnect hook lifecycle."""
    resource = SessionResource()
    socket = SessionSocket()
    router = session_router(resource, receive_loop=True)
    observed: list[tuple[str, int | None, int]] = []

    def before_disconnect(context: HookContext) -> None:
        observed.append((
            str(context.event),
            context.close_code,
            len(resource.disconnects),
        ))

    router.global_hooks.add("before_disconnect", before_disconnect)
    await router.on_websocket(make_req("/session"), socket)
    assert observed == [("before_disconnect", 1001, 0)], (
        "hooks must precede resource cleanup"
    )
    assert resource.disconnects == [(socket, 1001)], "hooks must not duplicate cleanup"


@pytest.mark.asyncio
async def test_receive_loop_failing_disconnect_hook_cannot_skip_cleanup() -> None:
    """Cleanup runs once even when a before-disconnect hook raises."""
    resource = SessionResource()
    socket = SessionSocket()
    router = session_router(resource, receive_loop=True)

    def before_disconnect(context: HookContext) -> None:
        msg = "disconnect hook failed"
        raise RuntimeError(msg)

    router.global_hooks.add("before_disconnect", before_disconnect)
    with pytest.raises(RuntimeError, match="disconnect hook failed"):
        await router.on_websocket(make_req("/session"), socket)
    assert resource.disconnects == [(socket, 1001)], (
        "hook failure must never skip cleanup"
    )


@pytest.mark.asyncio
async def test_receive_loop_cancellation_survives_cleanup_failure() -> None:
    """An ordinary cleanup failure cannot replace the caller's cancellation."""

    class FailingCleanupResource(SessionResource):
        @typ.override
        async def on_disconnect(self, ws: object, close_code: int) -> None:
            await super().on_disconnect(ws, close_code)
            msg = "cleanup failed"
            raise RuntimeError(msg)

    resource = FailingCleanupResource()
    socket = SessionSocket(None)
    router = session_router(resource, receive_loop=True)
    task = asyncio.create_task(router.on_websocket(make_req("/session"), socket))
    try:
        async with asyncio.timeout(1):
            await socket.receiving.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert resource.disconnects == [(socket, 1006)], (
            "failing cleanup must run only once"
        )
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_receive_loop_logs_dispatch_failure_without_exception_values(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Failure diagnostics expose the exception type without reflecting payloads."""
    resource = SessionResource(dispatch_error=RuntimeError("CANARY-dispatch-secret"))
    socket = SessionSocket(("first",))
    router = session_router(resource, receive_loop=True)
    await router.on_websocket(make_req("/session"), socket)
    assert "RuntimeError" in caplog.text, "dispatch failures must leave a diagnostic"
    assert "CANARY-dispatch-secret" not in caplog.text, (
        "diagnostics must omit exception values"
    )
