"""Exactly-once router cleanup for cancelled or failed handshakes."""

from __future__ import annotations

import asyncio
import typing as typ

import falcon
import pytest

from falcon_pachinko.testing import WebSocketSimulator
from falcon_pachinko.unittests.helpers import make_req
from falcon_pachinko.unittests.router_session_helpers import (
    SessionResource,
    SessionSocket,
    session_router,
)

if typ.TYPE_CHECKING:
    from falcon_pachinko import HookContext, WebSocketLike


class FailingCleanupResource(SessionResource):
    """Record disconnect then fail, so cleanup idempotency is observable."""

    @typ.override
    async def on_disconnect(self, ws: object, close_code: int) -> None:
        """Record cleanup before simulating a failing resource hook."""
        await super().on_disconnect(ws, close_code)
        msg = "cleanup failed"
        raise RuntimeError(msg)


class PeerDisconnectingAcceptedResource(SessionResource):
    """Report a peer close while sending the initial accepted-session frame."""

    @typ.override
    async def on_session_start(self, ws: object) -> None:
        """Simulate the peer closing during post-acceptance setup."""
        raise falcon.WebSocketDisconnected(code=4007)


class SimulatorDisconnectingAcceptedResource(SessionResource):
    """Read an initial frame during accepted setup, as supported simulators do."""

    @typ.override
    async def on_session_start(self, ws: WebSocketLike) -> None:
        """Wait for the initial frame, propagating a simulator peer close."""
        await ws.receive_media()


class ClosingAcceptedResource(SessionResource):
    """Close the socket during accepted setup before receive ownership transfers."""

    @typ.override
    async def on_session_start(self, ws: WebSocketLike) -> None:
        """Close the accepted socket while the application owns cleanup."""
        await ws.close(code=4013)


class CancellingAcceptedResource(SessionResource):
    """Cancel while performing post-acceptance setup."""

    @typ.override
    async def on_session_start(self, ws: object) -> None:
        raise asyncio.CancelledError


class FailingAcceptedResource(SessionResource):
    """Fail after Falcon has accepted the connection."""

    @typ.override
    async def on_session_start(self, ws: object) -> None:
        msg = "accepted setup failed"
        raise RuntimeError(msg)


class RejectingResource(SessionResource):
    """Reject the connection before acceptance or frame reception."""

    @typ.override
    async def on_connect(self, req: object, ws: object, **params: object) -> bool:
        return False


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
    router = session_router(resource)
    task = asyncio.create_task(router.on_websocket(make_req("/session"), socket))
    try:
        async with asyncio.timeout(1):
            await socket.receiving.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert resource.disconnects == [(resource.websocket, 1006)], (
            "cancellation must trigger cleanup with abnormal-close metadata"
        )
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_accepted_setup_cancellation_reports_1006_without_closing() -> None:
    """Cancellation after acceptance cleans up without sending reserved 1006."""
    resource = CancellingAcceptedResource()
    socket = SessionSocket()
    router = session_router(resource)
    observed_codes: list[int | None] = []
    router.global_hooks.add(
        "before_disconnect", lambda context: observed_codes.append(context.close_code)
    )

    with pytest.raises(asyncio.CancelledError):
        await router.on_websocket(make_req("/session"), socket)

    assert socket.accepted, "accepted setup cancellation must follow ws.accept()"
    assert not socket.closed, "cancellation must not send reserved close code 1006"
    assert observed_codes == [1006], "disconnect hooks must receive code 1006"
    assert resource.disconnects == [(resource.websocket, 1006)], (
        "resource cleanup must receive cancellation metadata exactly once"
    )


@pytest.mark.asyncio
async def test_accepted_setup_failure_closes_1011_and_cleans_up() -> None:
    """Unexpected accepted-setup failures close and finalize the session."""
    resource = FailingAcceptedResource()
    socket = SessionSocket()
    router = session_router(resource)

    with pytest.raises(RuntimeError, match="accepted setup failed"):
        await router.on_websocket(make_req("/session"), socket)

    assert socket.accepted, "accepted setup must run after the handshake"
    assert socket.close_codes == [1011], "accepted setup failure must close with 1011"
    assert resource.disconnects == [(resource.websocket, 1011)], (
        "disconnect cleanup must receive the post-accept failure code"
    )


@pytest.mark.asyncio
async def test_rejection_does_not_accept_or_receive_frames() -> None:
    """A rejected connection must return without starting the receive loop."""
    resource = RejectingResource()
    socket = SessionSocket(None)
    router = session_router(resource)

    await router.on_websocket(make_req("/session"), socket)

    assert not socket.accepted, "rejected connections must not be accepted"
    assert socket.receive_calls == 0, "rejection must not start frame reception"
    assert not resource.disconnects, "rejected connections must skip disconnect hooks"


@pytest.mark.asyncio
async def test_acceptance_failure_does_not_finalize_an_unaccepted_session() -> None:
    """A failed handshake never enters accepted-session cleanup."""
    resource = SessionResource()
    socket = SessionSocket(accept_error=RuntimeError("accept failed"))
    router = session_router(resource)
    with pytest.raises(RuntimeError, match="accept failed"):
        await router.on_websocket(make_req("/session"), socket)
    assert not resource.disconnects, "a failed handshake never established a session"
    assert socket.closed, "accept failure must use the existing error close path"


@pytest.mark.asyncio
async def test_accepted_setup_peer_disconnect_uses_peer_code_without_reclosing() -> (
    None
):
    """Peer closure during accepted setup uses normal ordered cleanup."""
    resource = PeerDisconnectingAcceptedResource()
    socket = SessionSocket()
    router = session_router(resource)
    observed_codes: list[int | None] = []
    router.global_hooks.add(
        "before_disconnect", lambda context: observed_codes.append(context.close_code)
    )

    await router.on_websocket(make_req("/session"), socket)

    assert socket.accepted, "on_session_start must run only after the handshake"
    assert not socket.closed, "peer disconnect cleanup must not close a closed peer"
    assert observed_codes == [4007], "disconnect hooks must receive the peer code"
    assert resource.disconnects == [(resource.websocket, 4007)], (
        "resource cleanup must receive the peer code exactly once"
    )


@pytest.mark.asyncio
async def test_simulator_peer_disconnect_during_accepted_setup_uses_peer_code() -> None:
    """Simulator disconnects during accepted setup retain normal close metadata."""
    resource = SimulatorDisconnectingAcceptedResource()
    socket = WebSocketSimulator()
    await socket.push_disconnect(4012)
    router = session_router(resource)
    observed_codes: list[int | None] = []
    router.global_hooks.add(
        "before_disconnect", lambda context: observed_codes.append(context.close_code)
    )

    await router.on_websocket(make_req("/session"), socket)

    assert socket.accepted, "accepted setup must start after the handshake"
    assert socket.close_code == 4012, "the simulator must keep the peer close code"
    assert observed_codes == [4012], "disconnect hooks must receive the peer close code"
    assert resource.disconnects == [(resource.websocket, 4012)], (
        "resource cleanup must receive the peer code exactly once"
    )


@pytest.mark.asyncio
async def test_after_connect_failure_does_not_finalize_before_acceptance() -> None:
    """Pre-accept hook errors do not run accepted-session cleanup."""
    resource = SessionResource()
    socket = SessionSocket()
    router = session_router(resource)

    def fail_after_connect(context: HookContext) -> None:
        """Inject a post-connect failure after resource state is acquired."""
        msg = "after_connect failed"
        raise RuntimeError(msg)

    router.global_hooks.add("after_connect", fail_after_connect)
    with pytest.raises(RuntimeError, match="after_connect failed"):
        await router.on_websocket(make_req("/session"), socket)
    assert not resource.disconnects, (
        "pre-accept hook failure never established a session"
    )
    assert socket.closed, "hook failure must use the existing error close path"


@pytest.mark.asyncio
async def test_receive_loop_runs_disconnect_hooks_before_cleanup() -> None:
    """Router-owned sessions retain the public before-disconnect hook lifecycle."""
    resource = SessionResource()
    socket = SessionSocket()
    router = session_router(resource)
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
    assert resource.disconnects == [(resource.websocket, 4000)], (
        "hooks must not duplicate cleanup"
    )


@pytest.mark.asyncio
async def test_rejected_connection_does_not_run_disconnect_hook() -> None:
    """Rejected pre-accept decisions never enter disconnect cleanup."""

    class RejectingResource(SessionResource):
        async def on_connect(self, req: object, ws: object, **params: object) -> bool:
            """Reject the handshake before the router accepts it."""
            del req, ws, params
            return False

    resource = RejectingResource()
    socket = SessionSocket()
    router = session_router(resource)
    hook_calls: list[int | None] = []

    def before_disconnect(context: HookContext) -> None:
        """Record that rejected connections never run this hook."""
        hook_calls.append(context.close_code)

    router.global_hooks.add("before_disconnect", before_disconnect)
    session = await router.negotiate(make_req("/session"), socket)
    assert session is None, "a false on_connect result must reject acceptance"
    assert not resource.disconnects, "rejected sessions must not run disconnect cleanup"
    assert not hook_calls, "rejected sessions must not run disconnect hooks"
    assert socket.closed, "the rejected transport must be closed"


@pytest.mark.asyncio
async def test_receive_loop_logs_dispatch_failure_without_exception_values(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Failure diagnostics expose the exception type without reflecting payloads."""
    resource = SessionResource(dispatch_error=RuntimeError("CANARY-dispatch-secret"))
    socket = SessionSocket(("first",))
    router = session_router(resource)
    with pytest.raises(RuntimeError, match="CANARY-dispatch-secret"):
        await router.on_websocket(make_req("/session"), socket)
    assert "RuntimeError" in caplog.text, "dispatch failures must leave a diagnostic"
    assert "CANARY-dispatch-secret" not in caplog.text, (
        "diagnostics must omit exception values"
    )


@pytest.mark.asyncio
async def test_receive_loop_dispatches_frames_in_order_and_disconnects_once() -> None:
    """Strings and bytes pass unchanged; decoded media becomes JSON bytes."""
    resource = SessionResource()
    socket = SessionSocket(("text", b"binary", {"value": 1}))
    router = session_router(resource)
    await router.on_websocket(make_req("/session"), socket)
    assert resource.frames == ["text", b"binary", b'{"value":1}'], (
        "frames must dispatch in receive order"
    )
    assert resource.disconnects == [(resource.websocket, 4000)], (
        "peer close code must reach one cleanup call"
    )
    assert socket.accepted, "the receive loop must follow acceptance"


@pytest.mark.asyncio
async def test_receive_loop_dispatch_failure_closes_and_disconnects_once() -> None:
    """Dispatch errors retain the shared driver's policy and original exception."""
    failure = RuntimeError("dispatch failed")
    resource = SessionResource(dispatch_error=failure)
    socket = SessionSocket(("first", "second"))
    router = session_router(resource)
    with pytest.raises(RuntimeError, match="dispatch failed") as caught:
        await router.on_websocket(make_req("/session"), socket)
    assert caught.value is failure, "the shared session must preserve error identity"
    assert socket.closed, "dispatch failure must close the socket"
    assert resource.frames == ["first"], "dispatch failure must end the receive loop"
    assert resource.disconnects == [(resource.websocket, 1011)], (
        "the router close code must reach one cleanup call"
    )


@pytest.mark.asyncio
async def test_manual_negotiation_returns_before_session_start() -> None:
    """Manual negotiation accepts without starting the receive owner."""
    resource = SessionResource()
    socket = SessionSocket(None)
    router = session_router(resource)
    session = await router.negotiate(make_req("/session"), socket)
    assert session is not None, "manual negotiation should return its session"
    assert socket.accepted, "manual negotiation should accept the transport"
    assert socket.receive_calls == 0, "negotiation must not receive frames"
    assert not resource.disconnects, "negotiation must not finalize the session"
    await session.aclose()
    assert resource.disconnects == [(resource.websocket, 1000)], (
        "closing a manual session must run disconnect cleanup once"
    )


@pytest.mark.asyncio
async def test_setup_close_runs_router_owned_cleanup() -> None:
    """A setup-time close is finalized by the router-owned session."""
    resource = ClosingAcceptedResource()
    socket = SessionSocket()
    router = session_router(resource)

    await router.on_websocket(make_req("/session"), socket)

    assert socket.accepted, "accepted setup must run after the handshake"
    assert socket.close_codes == [4013], "setup must retain its close operation"
    assert len(resource.disconnects) == 1, "the router must finalize the closed session"


@pytest.mark.asyncio
async def test_session_owner_cleanup_failure_is_idempotent() -> None:
    """A failed manual session cleanup is not attempted a second time."""
    resource = FailingCleanupResource()
    socket = SessionSocket()
    router = session_router(resource)
    session = await router.negotiate(make_req("/session"), socket)
    assert session is not None, "manual negotiation should return its session"

    with pytest.raises(RuntimeError, match="cleanup failed"):
        await session.aclose()

    with pytest.raises(RuntimeError, match="cleanup failed"):
        await session.aclose()

    assert resource.disconnects == [(resource.websocket, 1000)], (
        "the cleanup callback must run only once despite its failure"
    )
