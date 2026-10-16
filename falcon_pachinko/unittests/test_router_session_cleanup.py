"""Exactly-once router cleanup for cancelled or failed handshakes."""

from __future__ import annotations

import asyncio
import typing as typ

import pytest

from falcon_pachinko.hooks import HookCollection, HookManager
from falcon_pachinko.testing._router_session import _ConnectionSession
from falcon_pachinko.unittests.helpers import make_req
from falcon_pachinko.unittests.router_session_helpers import (
    SessionResource,
    SessionSocket,
    session_router,
)

if typ.TYPE_CHECKING:
    from falcon_pachinko import HookContext


class FailingCleanupResource(SessionResource):
    """Record disconnect then fail, so cleanup idempotency is observable."""

    @typ.override
    async def on_disconnect(self, ws: object, close_code: int) -> None:
        """Record cleanup before simulating a failing resource hook."""
        await super().on_disconnect(ws, close_code)
        msg = "cleanup failed"
        raise RuntimeError(msg)


@pytest.mark.parametrize(
    "resource_type",
    [SessionResource, FailingCleanupResource],
    ids=["cleanup-succeeds", "cleanup-fails"],
)
@pytest.mark.asyncio
async def test_receive_loop_cancellation_disconnects_once(
    resource_type: type[SessionResource],
) -> None:
    """Cancellation cleans up once even if cleanup fails, and still propagates."""
    resource = resource_type()
    socket = SessionSocket(None)
    router = session_router(resource, receive_loop=True)
    task = asyncio.create_task(router.on_websocket(make_req("/session"), socket))
    try:
        async with asyncio.timeout(1):
            await socket.receiving.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert resource.disconnects == [(socket, 1001)], (
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
    assert resource.disconnects == [(socket, 1011)], (
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
        """Inject a post-connect failure after resource state is acquired."""
        msg = "after_connect failed"
        raise RuntimeError(msg)

    router.global_hooks.add("after_connect", fail_after_connect)
    with pytest.raises(RuntimeError, match="after_connect failed"):
        await router.on_websocket(make_req("/session"), socket)
    assert resource.disconnects == [(socket, 1011)], (
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
        """Capture hook order and state before resource cleanup runs."""
        observed.append((
            str(context.event),
            context.close_code,
            len(resource.disconnects),
        ))

    router.global_hooks.add("before_disconnect", before_disconnect)
    await router.on_websocket(make_req("/session"), socket)
    assert observed == [("before_disconnect", 4000, 0)], (
        "hooks must precede resource cleanup"
    )
    assert resource.disconnects == [(socket, 4000)], "hooks must not duplicate cleanup"


@pytest.mark.asyncio
async def test_receive_loop_failing_disconnect_hook_cannot_skip_cleanup() -> None:
    """Cleanup runs once even when a before-disconnect hook raises."""
    resource = SessionResource()
    socket = SessionSocket()
    router = session_router(resource, receive_loop=True)

    def before_disconnect(context: HookContext) -> None:
        """Raise from the hook to verify cleanup remains protected."""
        msg = "disconnect hook failed"
        raise RuntimeError(msg)

    router.global_hooks.add("before_disconnect", before_disconnect)
    with pytest.raises(RuntimeError, match="disconnect hook failed"):
        await router.on_websocket(make_req("/session"), socket)
    assert resource.disconnects == [(socket, 4000)], (
        "hook failure must never skip cleanup"
    )


@pytest.mark.asyncio
async def test_receive_loop_propagates_dispatch_failure_without_duplicate_logging(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The router propagates failures for the outer request boundary to log."""
    failure = RuntimeError("CANARY-dispatch-secret")
    resource = SessionResource(dispatch_error=failure)
    socket = SessionSocket(("first",))
    router = session_router(resource, receive_loop=True)
    with pytest.raises(RuntimeError, match="CANARY-dispatch-secret") as captured:
        await router.on_websocket(make_req("/session"), socket)
    assert captured.value is failure, "dispatch failure must propagate unchanged"
    assert not any(
        record.name == "falcon_pachinko._session_driver" for record in caplog.records
    ), "the session driver must not duplicate the request boundary's error log"
    assert "CANARY-dispatch-secret" not in caplog.text, (
        "the router must not log exception values before propagation"
    )


@pytest.mark.asyncio
async def test_receive_loop_dispatches_frames_in_order_and_disconnects_once() -> None:
    """Strings and bytes pass unchanged; decoded media becomes JSON bytes."""
    resource = SessionResource()
    socket = SessionSocket(("text", b"binary", {"value": 1}))
    router = session_router(resource, receive_loop=True)
    await router.on_websocket(make_req("/session"), socket)
    assert resource.frames == ["text", b"binary", b'{"value":1}'], (
        "frames must dispatch in receive order"
    )
    assert resource.disconnects == [(socket, 4000)], (
        "peer close code must reach one cleanup call"
    )
    assert socket.accepted, "the receive loop must follow acceptance"


@pytest.mark.asyncio
async def test_receive_loop_dispatch_failure_closes_and_disconnects_once() -> None:
    """Dispatch errors retain the shared driver's policy and original exception."""
    failure = RuntimeError("dispatch failed")
    resource = SessionResource(dispatch_error=failure)
    socket = SessionSocket(("first", "second"))
    router = session_router(resource, receive_loop=True)
    with pytest.raises(RuntimeError, match="dispatch failed") as caught:
        await router.on_websocket(make_req("/session"), socket)
    assert caught.value is failure, "the shared session must preserve error identity"
    assert socket.closed, "dispatch failure must close the socket"
    assert resource.frames == ["first"], "dispatch failure must end the receive loop"
    assert resource.disconnects == [(socket, 1011)], (
        "the router close code must reach one cleanup call"
    )


@pytest.mark.asyncio
async def test_disabled_receive_loop_preserves_immediate_return() -> None:
    """Existing applications own receive and cleanup when the option is off."""
    resource = SessionResource()
    socket = SessionSocket(None)
    router = session_router(resource, receive_loop=False)
    async with asyncio.timeout(1):
        await router.on_websocket(make_req("/session"), socket)
    assert socket.accepted, "the default path must still accept"
    assert socket.receive_calls == 0, "the default path must not receive frames"
    assert not resource.disconnects, (
        "the default path must retain existing lifecycle ownership"
    )


@pytest.mark.asyncio
async def test_session_owner_cleanup_failure_is_idempotent() -> None:
    """A failed cleanup callback is not retried when the owner exits again."""
    resource = FailingCleanupResource()
    socket = SessionSocket()
    hook_manager = HookManager(
        global_hooks=HookCollection(),
        resources=[resource],
    )
    session = _ConnectionSession(resource, socket, hook_manager)

    with pytest.raises(RuntimeError, match="cleanup failed"):
        async with session:
            pass

    async with session:
        pass

    assert resource.disconnects == [(socket, 1006)], (
        "the cleanup callback must run only once despite its failure"
    )
