"""Exactly-once router cleanup for cancelled or failed handshakes."""

from __future__ import annotations

import asyncio
import logging
import typing as typ

import falcon
import pytest

from falcon_pachinko import WebSocketResource, WebSocketRouter
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
    async def on_accepted(self, ws: object) -> None:
        """Simulate the peer closing during post-acceptance setup."""
        raise falcon.WebSocketDisconnected(code=4007)


class SimulatorDisconnectingAcceptedResource(SessionResource):
    """Read an initial frame during accepted setup, as supported simulators do."""

    @typ.override
    async def on_accepted(self, ws: WebSocketLike) -> None:
        """Wait for the initial frame, propagating a simulator peer close."""
        await ws.receive_media()


class ClosingAcceptedResource(SessionResource):
    """Close the socket during accepted setup before receive ownership transfers."""

    @typ.override
    async def on_accepted(self, ws: WebSocketLike) -> None:
        """Close the accepted socket while the application owns cleanup."""
        await ws.close(code=4013)


class CancellingAcceptedResource(SessionResource):
    """Cancel while performing post-acceptance setup."""

    @typ.override
    async def on_accepted(self, ws: object) -> None:
        raise asyncio.CancelledError


class FailingAcceptedResource(SessionResource):
    """Fail after Falcon has accepted the connection."""

    @typ.override
    async def on_accepted(self, ws: object) -> None:
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
    router = session_router(resource, receive_loop=True)
    task = asyncio.create_task(router.on_websocket(make_req("/session"), socket))
    try:
        async with asyncio.timeout(1):
            await socket.receiving.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert resource.disconnects == [(socket, 1006)], (
            "cancellation must trigger cleanup with abnormal-close metadata"
        )
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("receive_loop", [False, True])
@pytest.mark.asyncio
async def test_accepted_setup_cancellation_reports_1006_without_closing(
    *,
    receive_loop: bool,
) -> None:
    """Cancellation after acceptance cleans up without sending reserved 1006."""
    resource = CancellingAcceptedResource()
    socket = SessionSocket()
    router = session_router(resource, receive_loop=receive_loop)
    observed_codes: list[int | None] = []
    router.global_hooks.add(
        "before_disconnect", lambda context: observed_codes.append(context.close_code)
    )

    with pytest.raises(asyncio.CancelledError):
        await router.on_websocket(make_req("/session"), socket)

    assert socket.accepted, "accepted setup cancellation must follow ws.accept()"
    assert not socket.closed, "cancellation must not send reserved close code 1006"
    assert observed_codes == [1006], "disconnect hooks must receive code 1006"
    assert resource.disconnects == [(socket, 1006)], (
        "resource cleanup must receive cancellation metadata exactly once"
    )


@pytest.mark.parametrize("receive_loop", [False, True])
@pytest.mark.asyncio
async def test_accepted_setup_failure_closes_1011_and_cleans_up(
    *,
    receive_loop: bool,
) -> None:
    """Unexpected accepted-setup failures close and finalize the session."""
    resource = FailingAcceptedResource()
    socket = SessionSocket()
    router = session_router(resource, receive_loop=receive_loop)

    with pytest.raises(RuntimeError, match="accepted setup failed"):
        await router.on_websocket(make_req("/session"), socket)

    assert socket.accepted, "accepted setup must run after the handshake"
    assert socket.close_codes == [1011], "accepted setup failure must close with 1011"
    assert resource.disconnects == [(socket, 1011)], (
        "disconnect cleanup must receive the post-accept failure code"
    )


@pytest.mark.asyncio
async def test_rejection_does_not_accept_or_receive_frames() -> None:
    """A rejected connection must return without starting the receive loop."""
    resource = RejectingResource()
    socket = SessionSocket(None)
    router = session_router(resource, receive_loop=True)

    await router.on_websocket(make_req("/session"), socket)

    assert not socket.accepted, "rejected connections must not be accepted"
    assert socket.receive_calls == 0, "rejection must not start frame reception"
    assert not resource.disconnects, "rejected connections must skip disconnect hooks"


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
    assert not socket.closed, "an unaccepted socket cannot send a close frame"


@pytest.mark.parametrize("receive_loop", [False, True])
@pytest.mark.asyncio
async def test_accepted_setup_peer_disconnect_uses_peer_code_without_reclosing(
    *,
    receive_loop: bool,
) -> None:
    """Peer closure during accepted setup uses normal ordered cleanup."""
    resource = PeerDisconnectingAcceptedResource()
    socket = SessionSocket()
    router = session_router(resource, receive_loop=receive_loop)
    observed_codes: list[int | None] = []
    router.global_hooks.add(
        "before_disconnect", lambda context: observed_codes.append(context.close_code)
    )

    await router.on_websocket(make_req("/session"), socket)

    assert socket.accepted, "on_accepted must run only after the handshake"
    assert not socket.closed, "peer disconnect cleanup must not close a closed peer"
    assert observed_codes == [4007], "disconnect hooks must receive the peer code"
    assert resource.disconnects == [(socket, 4007)], (
        "resource cleanup must receive the peer code exactly once"
    )


@pytest.mark.parametrize("receive_loop", [False, True])
@pytest.mark.asyncio
async def test_simulator_peer_disconnect_during_accepted_setup_uses_peer_code(
    *,
    receive_loop: bool,
) -> None:
    """Simulator disconnects during accepted setup retain normal close metadata."""
    resource = SimulatorDisconnectingAcceptedResource()
    socket = WebSocketSimulator()
    await socket.push_disconnect(4012)
    router = session_router(resource, receive_loop=receive_loop)
    observed_codes: list[int | None] = []
    router.global_hooks.add(
        "before_disconnect", lambda context: observed_codes.append(context.close_code)
    )

    await router.on_websocket(make_req("/session"), socket)

    assert socket.accepted, "accepted setup must start after the handshake"
    assert socket.close_code == 4012, "the simulator must keep the peer close code"
    assert observed_codes == [4012], "disconnect hooks must receive the peer close code"
    assert resource.disconnects == [(socket, 4012)], (
        "resource cleanup must receive the peer code exactly once"
    )


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
    assert resource.disconnects == [(socket, 1006)], (
        "hook failure must clean up registered state once"
    )
    assert not socket.closed, "an unaccepted socket cannot send a close frame"


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
async def test_setup_only_handoff_allows_application_owned_dispatch() -> None:
    """Application-owned receives can dispatch after setup returns."""

    class ApplicationOwnedDispatchResource(WebSocketResource):
        def __init__(self) -> None:
            self.websocket: WebSocketLike | None = None
            self.unhandled: list[str | bytes] = []

        @typ.override
        async def on_accepted(self, ws: WebSocketLike) -> None:
            self.websocket = ws

        @typ.override
        async def on_unhandled(self, ws: WebSocketLike, message: str | bytes) -> None:
            self.unhandled.append(message)

    resource = ApplicationOwnedDispatchResource()
    router = WebSocketRouter(receive_loop=False)
    router.add_route("/session", lambda: resource)
    router.mount("/")
    await router.on_websocket(make_req("/session"), SessionSocket())

    websocket = resource.websocket
    assert websocket is not None, "setup must retain the accepted application socket"
    await resource.dispatch(websocket, "application-owned frame")

    assert resource.unhandled == ["application-owned frame"], (
        "the completed router setup must release its receive guard to the app"
    )


@pytest.mark.asyncio
async def test_receive_loop_opt_out_cleans_up_a_setup_closed_socket() -> None:
    """A setup-time close ends router-owned setup and requires cleanup."""
    resource = ClosingAcceptedResource()
    socket = SessionSocket()
    router = session_router(resource, receive_loop=False)

    await router.on_websocket(make_req("/session"), socket)

    assert socket.accepted, "accepted setup must run after the handshake"
    assert socket.close_codes == [4013], "setup must retain its close operation"
    assert resource.disconnects == [(socket, 4013)], (
        "cleanup must retain the transport's reported close code"
    )


@pytest.mark.asyncio
async def test_session_cleanup_observation_is_emitted_once(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Production routing emits one completed cleanup observation."""
    caplog.set_level(logging.DEBUG, logger="falcon_pachinko._session_driver")
    resource = SessionResource()
    socket = SessionSocket()
    await session_router(resource, receive_loop=True).on_websocket(
        make_req("/session"), socket
    )

    completed = [
        record
        for record in caplog.records
        if getattr(record, "operation", None) == "cleanup"
        and vars(record)["outcome"] == "completed"
    ]
    assert len(completed) == 1, "production routing must report one cleanup"
    assert resource.disconnects == [(socket, 4000)], (
        "production routing must invoke resource cleanup once"
    )
