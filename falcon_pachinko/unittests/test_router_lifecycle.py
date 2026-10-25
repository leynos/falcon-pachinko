"""Tests for WebSocketRouter connection lifecycle."""

from __future__ import annotations

import asyncio
import typing as typ
from unittest import mock

import falcon
import pytest

from falcon_pachinko import (
    AcceptOptions,
    HookContext,
    LifecycleError,
    WebSocketResource,
    WebSocketRouter,
)
from falcon_pachinko._disconnect import _PeerDisconnectedError
from falcon_pachinko.unittests._router_test_resources import (
    AcceptingResource,
    DummyResource,
)
from falcon_pachinko.unittests.helpers import RecordingWS, make_req


@pytest.mark.asyncio
async def test_negotiate_accepts_options_once_and_returns_session() -> None:
    """Negotiation applies resource options through one framework acceptance."""

    class OptionResource(WebSocketResource):
        async def on_connect(
            self, req: object, ws: object, **params: object
        ) -> AcceptOptions:
            del req, ws, params
            return AcceptOptions(subprotocol="chat", headers=(("x-session", "ready"),))

    router = WebSocketRouter()
    router.add_route("/options", OptionResource)
    router.mount("/")
    ws = RecordingWS()

    session = await router.negotiate(make_req("/options"), ws)

    assert session is not None, "approved negotiation should return a session"
    assert ws.accept_calls == [("chat", (("x-session", "ready"),))], (
        "the router should pass options to exactly one accept call"
    )
    assert session.ws.phase.value == "accepted", "the session socket is accepted"
    await session.aclose()


@pytest.mark.asyncio
async def test_negotiate_rejection_closes_without_accepting() -> None:
    """A falsey pre-accept decision closes once and returns no session."""
    router = WebSocketRouter()
    router.add_route("/reject", DummyResource)
    router.mount("/")
    ws = RecordingWS()

    session = await router.negotiate(make_req("/reject"), ws)

    assert session is None, "rejected negotiation must not return a session"
    assert not ws.accept_calls, "rejection must not accept the WebSocket"
    assert ws.closed == [1000], "rejection must close the WebSocket exactly once"


@pytest.mark.asyncio
async def test_invalid_connection_decision_runs_after_connect_with_error() -> None:
    """Invalid decision values still complete the paired setup hooks."""
    hook_contexts: list[HookContext] = []

    class InvalidDecisionResource(WebSocketResource):
        async def on_connect(self, req: object, ws: object, **params: object) -> bool:
            del req, ws, params
            return typ.cast("bool", 1)

    router = WebSocketRouter()
    router.add_route("/invalid-decision", InvalidDecisionResource)
    router.mount("/")
    router.global_hooks.add("after_connect", hook_contexts.append)
    ws = RecordingWS()

    with pytest.raises(TypeError, match="on_connect must return") as raised:
        await router.negotiate(make_req("/invalid-decision"), ws)

    assert len(hook_contexts) == 1, "after-connect must pair with before-connect"
    assert hook_contexts[0].error is raised.value, "the hook must see the failure"
    assert hook_contexts[0].result is False, "invalid decisions are rejected"
    assert not ws.accept_calls, "invalid decisions must not accept the socket"
    assert ws.closed == [1000], "invalid decisions must close before acceptance"


@pytest.mark.asyncio
async def test_deprecated_resource_accept_is_not_repeated() -> None:
    """Legacy resource acceptance warns and is not repeated by the router."""

    class LegacyResource(WebSocketResource):
        async def on_connect(self, req: object, ws: object, **params: object) -> bool:
            del req, params
            await typ.cast("RecordingWS", ws).accept(subprotocol="legacy")
            return True

    router = WebSocketRouter()
    router.add_route("/legacy", LegacyResource)
    router.mount("/")
    ws = RecordingWS()

    with pytest.warns(DeprecationWarning, match="router-owned"):
        session = await router.negotiate(make_req("/legacy"), ws)

    assert session is not None, "legacy acceptance should remain compatible"
    assert len(ws.accept_calls) == 1, "the same socket must never be accepted twice"
    await session.aclose()


@pytest.mark.asyncio
async def test_pre_accept_send_names_session_start_as_supported_phase() -> None:
    """Pre-accept sends fail with a migration hint and close the request."""

    class SendingResource(WebSocketResource):
        async def on_connect(self, req: object, ws: object, **params: object) -> bool:
            del req, params
            await typ.cast("RecordingWS", ws).send_media("too early")
            return True

    router = WebSocketRouter()
    router.add_route("/too-early", SendingResource)
    router.mount("/")
    ws = RecordingWS()

    with pytest.raises(LifecycleError, match="on_session_start"):
        await router.negotiate(make_req("/too-early"), ws)

    assert not ws.accept_calls, "a failed pre-accept send must not accept"
    assert ws.closed == [1000], "a pre-accept failure must close once"


@pytest.mark.asyncio
async def test_session_start_failure_closes_and_disconnects_once() -> None:
    """An accepted session-start failure closes with 1011 and cleans up once."""
    error = RuntimeError("session start failed")

    class FailingResource(WebSocketResource):
        disconnect_codes: typ.ClassVar[list[int]] = []

        async def on_connect(self, req: object, ws: object, **params: object) -> bool:
            return True

        async def on_session_start(self, ws: object) -> None:
            del ws
            raise error

        async def on_disconnect(self, ws: object, close_code: int) -> None:
            del ws
            self.disconnect_codes.append(close_code)

    router = WebSocketRouter()
    router.add_route("/start-failure", FailingResource)
    router.mount("/")
    ws = RecordingWS()

    with pytest.raises(RuntimeError, match="session start failed"):
        await router.on_websocket(make_req("/start-failure"), ws)

    assert ws.closed == [1011], "session-start errors must close with 1011"
    assert FailingResource.disconnect_codes == [1011], (
        "accepted startup failures must finalize exactly once"
    )


@pytest.mark.asyncio
async def test_setup_failure_preserves_existing_exception_cause() -> None:
    """Pre-accept re-raising keeps the application's explicit cause chain."""
    cause = ValueError("root setup cause")
    error = RuntimeError("setup failed")
    error.__cause__ = cause

    class FailingResource(WebSocketResource):
        async def on_connect(self, req: object, ws: object, **params: object) -> bool:
            del req, ws, params
            raise error

    router = WebSocketRouter()
    router.add_route("/setup-failure", FailingResource)
    router.mount("/")

    with pytest.raises(RuntimeError, match="setup failed") as raised:
        await router.negotiate(make_req("/setup-failure"), RecordingWS())

    assert raised.value is error, "the original setup exception must propagate"
    assert raised.value.__cause__ is cause, "its explicit cause must remain attached"


@pytest.mark.asyncio
async def test_legacy_disconnect_propagates_finalization_failure() -> None:
    """An accepted disconnect without a primary error must expose cleanup errors."""
    cleanup_error = RuntimeError("disconnect cleanup failed")

    class LegacyDisconnectedResource(WebSocketResource):
        async def on_connect(self, req: object, ws: object, **params: object) -> bool:
            del req, params
            await typ.cast("typ.Any", ws).accept()
            raise falcon.WebSocketDisconnected(1000)

        async def on_disconnect(self, ws: object, close_code: int) -> None:
            del ws, close_code
            raise cleanup_error

    router = WebSocketRouter()
    router.add_route("/legacy-disconnect", LegacyDisconnectedResource)
    router.mount("/")

    with (
        pytest.warns(DeprecationWarning, match="router-owned"),
        pytest.raises(RuntimeError, match="disconnect cleanup failed") as raised,
    ):
        await router.negotiate(make_req("/legacy-disconnect"), RecordingWS())

    assert raised.value is cleanup_error, "the finalization failure must propagate"


@pytest.mark.asyncio
async def test_session_failure_preserves_cause_through_finalization_and_close() -> None:
    """Session and explicit-close re-raises retain application exception causes."""
    cause = ValueError("root session cause")
    error = RuntimeError("session failed")
    error.__cause__ = cause

    class FailingResource(WebSocketResource):
        async def on_connect(self, req: object, ws: object, **params: object) -> bool:
            del req, ws, params
            return True

        async def on_session_start(self, ws: object) -> None:
            del ws
            raise error

    router = WebSocketRouter()
    router.add_route("/session-failure", FailingResource)
    router.mount("/")
    session = await router.negotiate(make_req("/session-failure"), RecordingWS())
    assert session is not None, "the accepted session should be returned"

    with pytest.raises(RuntimeError, match="session failed") as raised:
        await session.run()

    assert raised.value is error, "session execution must re-raise the same error"
    assert raised.value.__cause__ is cause, "finalization must preserve its cause"

    with pytest.raises(RuntimeError, match="session failed") as closed:
        await session.aclose()

    assert closed.value is error, "explicit cleanup must retain the session error"
    assert closed.value.__cause__ is cause, "close must preserve the original cause"


@pytest.mark.asyncio
async def test_disconnect_failure_preserves_existing_exception_cause() -> None:
    """A disconnect callback failure keeps its explicit cause through cleanup."""
    cause = ValueError("root disconnect cause")
    error = RuntimeError("disconnect failed")
    error.__cause__ = cause

    class FailingResource(AcceptingResource):
        async def on_disconnect(self, ws: object, close_code: int) -> None:
            del ws, close_code
            raise error

    router = WebSocketRouter()
    router.add_route("/disconnect-failure", FailingResource)
    router.mount("/")
    session = await router.negotiate(make_req("/disconnect-failure"), RecordingWS())
    assert session is not None, "the accepted session should be returned"

    with pytest.raises(RuntimeError, match="disconnect failed") as raised:
        await session.aclose()

    assert raised.value is error, "the cleanup error must propagate unchanged"
    assert raised.value.__cause__ is cause, "cleanup must retain its explicit cause"


@pytest.mark.asyncio
async def test_after_connect_result_mutation_does_not_change_acceptance() -> None:
    """The post-hook may observe the decision but cannot rewrite it."""
    router = WebSocketRouter()
    router.add_route("/after-connect", AcceptingResource)
    router.mount("/")

    def mutate_result(context: HookContext) -> None:
        context.result = False

    router.global_hooks.add("after_connect", mutate_result)
    session = await router.negotiate(make_req("/after-connect"), RecordingWS())

    assert session is not None, "the original on_connect decision is authoritative"
    await session.aclose()


@pytest.mark.asyncio
async def test_guard_rejects_reaccept_and_close_is_idempotent() -> None:
    """The guarded lifecycle enforces one acceptance and one close."""
    router = WebSocketRouter()
    router.add_route("/guard", AcceptingResource)
    router.mount("/")
    ws = RecordingWS()
    session = await router.negotiate(make_req("/guard"), ws)
    assert session is not None, "the router must return the accepted session"

    with (
        pytest.warns(DeprecationWarning, match="router-owned"),
        pytest.raises(LifecycleError, match="accepted"),
    ):
        await session.ws.accept()

    await session.aclose(code=4001)
    await session.aclose(code=4002)

    assert len(ws.accept_calls) == 1, "re-acceptance must never reach the transport"
    assert ws.closed == [4001], "close is idempotent and retains its first code"
    assert session.ws.close_code == 4001, "the guard must retain the first close code"


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["receive_media", "receive_text"])
async def test_pre_accept_receive_requires_session_start(operation: str) -> None:
    """Receiving frames before the upgrade fails with migration guidance."""

    class ReceivingResource(WebSocketResource):
        async def on_connect(self, req: object, ws: object, **params: object) -> bool:
            del req, params
            receive = getattr(typ.cast("typ.Any", ws), operation)
            await receive()
            return True

    router = WebSocketRouter()
    router.add_route("/too-early", ReceivingResource)
    router.mount("/")
    ws = RecordingWS()

    with pytest.raises(LifecycleError, match="on_session_start"):
        await router.negotiate(make_req("/too-early"), ws)

    assert not ws.accept_calls, "pre-accept receive must not accept the socket"
    assert ws.closed == [1000], "pre-accept receive failure must close the request"


@pytest.mark.asyncio
async def test_peer_close_code_is_retained_by_session_guard() -> None:
    """Direct session reads preserve the peer close code for cleanup."""
    disconnect_codes: list[int] = []

    class DisconnectResource(AcceptingResource):
        async def on_disconnect(self, ws: object, close_code: int) -> None:
            del ws
            disconnect_codes.append(close_code)

    router = WebSocketRouter()
    router.add_route("/peer-close", DisconnectResource)
    router.mount("/")
    ws = RecordingWS(disconnect_code=4007)
    session = await router.negotiate(make_req("/peer-close"), ws)
    assert session is not None, "the router must return the accepted session"

    with pytest.raises(_PeerDisconnectedError):
        await session.ws.receive_json()
    await session.aclose()

    assert session.ws.close_code == 4007, "the guard must retain the peer close code"
    assert disconnect_codes == [4007], "disconnect must receive the peer close code"


@pytest.mark.asyncio
async def test_none_close_code_uses_normal_closure_code() -> None:
    """An omitted close code is reported as a normal 1000 closure."""
    disconnect_codes: list[int] = []

    class DisconnectResource(AcceptingResource):
        async def on_disconnect(self, ws: object, close_code: int) -> None:
            del ws
            disconnect_codes.append(close_code)

    router = WebSocketRouter()
    router.add_route("/default-close", DisconnectResource)
    router.mount("/")
    ws = RecordingWS()
    session = await router.negotiate(make_req("/default-close"), ws)
    assert session is not None, "the accepted session should be returned"

    await session.ws.close(None)
    await session.aclose()

    assert ws.closed == [1000], "None must be normalized before transport close"
    assert session.ws.close_code == 1000, "the guard must record normal closure"
    assert disconnect_codes == [1000], "disconnect must receive normal closure"


@pytest.mark.asyncio
async def test_guarded_binary_helpers_use_native_data_transport() -> None:
    """Direct binary helpers preserve Falcon's raw data frame API."""

    class NativeDataWS(RecordingWS):
        def __init__(self) -> None:
            super().__init__()
            self.send_data: mock.AsyncMock = mock.AsyncMock()
            self.receive_data: mock.AsyncMock = mock.AsyncMock(return_value=b"reply")

    router = WebSocketRouter()
    router.add_route("/binary-direct", AcceptingResource)
    router.mount("/")
    ws = NativeDataWS()
    session = await router.negotiate(make_req("/binary-direct"), ws)
    assert session is not None, "the accepted session should be returned"

    await session.ws.send_bytes(b"hello")
    reply = await session.ws.receive_bytes()
    await session.aclose()

    ws.send_data.assert_awaited_once_with(b"hello")
    ws.receive_data.assert_awaited_once_with()
    assert reply == b"reply", "binary receives should use native receive_data"


@pytest.mark.asyncio
async def test_aclose_waits_for_run_completion_not_caller_completion() -> None:
    """Explicit close must not wait for unrelated work in the run caller."""
    router = WebSocketRouter()
    router.add_route("/run-owner", AcceptingResource)
    router.mount("/")
    ws = RecordingWS(disconnect_code=1000)
    session = await router.negotiate(make_req("/run-owner"), ws)
    assert session is not None, "the accepted session should be returned"

    run_returned = asyncio.Event()
    continue_after_run = asyncio.Event()

    async def run_then_wait() -> None:
        await session.run()
        run_returned.set()
        await continue_after_run.wait()

    owner_task = asyncio.create_task(run_then_wait())
    try:
        await asyncio.wait_for(run_returned.wait(), timeout=1)
        await asyncio.wait_for(session.aclose(), timeout=1)
        assert not owner_task.done(), "session close must not await caller work"
    finally:
        continue_after_run.set()
        await owner_task


@pytest.mark.asyncio
async def test_session_cancellation_closes_with_going_away_and_finalizes() -> None:
    """Cancellation closes an accepted session with 1001 before cleanup."""
    started = asyncio.Event()
    disconnect_codes: list[int] = []

    class BlockingResource(AcceptingResource):
        async def on_session_start(self, ws: object) -> None:
            del ws
            started.set()
            await asyncio.Event().wait()

        async def on_disconnect(self, ws: object, close_code: int) -> None:
            del ws
            disconnect_codes.append(close_code)

    router = WebSocketRouter()
    router.add_route("/cancel", BlockingResource)
    router.mount("/")
    ws = RecordingWS()
    task = asyncio.create_task(router.on_websocket(make_req("/cancel"), ws))
    await started.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert ws.closed == [1001], "cancellation must close with going-away"
    assert disconnect_codes == [1001], "disconnect must observe cancellation close"
