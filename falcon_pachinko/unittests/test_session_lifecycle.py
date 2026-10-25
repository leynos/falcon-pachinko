"""Focused router-owned session lifecycle contracts."""

from __future__ import annotations

import logging
import typing as typ

import falcon
import pytest
from hypothesis import given
from hypothesis import strategies as st

from falcon_pachinko import WebSocketResource
from falcon_pachinko._session import SessionPhase, SessionState
from falcon_pachinko.hooks import HookCollection, HookEvent, HookManager
from falcon_pachinko.unittests._session_fixtures import (
    RecordingResource,
    SessionSocket,
    make_session_request,
    make_session_router,
)

_ASSERTION_FAILURE = "lifecycle expectation failed"
_SESSION_ACTIONS = ("establish", "disconnect", "close", "receive")


@pytest.mark.asyncio
async def test_rejected_connection_closes_without_disconnect_cleanup() -> None:
    """A falsey decision before acceptance rejects without post-accept cleanup."""
    socket = SessionSocket()

    class RejectingResource(RecordingResource):
        def __init__(self) -> None:
            super().__init__(accepted=False)

    router = make_session_router(RejectingResource)
    await router.on_websocket(
        typ.cast("typ.Any", make_session_request()), typ.cast("typ.Any", socket)
    )

    assert socket.unaccepted is True, _ASSERTION_FAILURE
    assert socket.close_codes == [1000], _ASSERTION_FAILURE
    resource = RejectingResource.instances[0]
    assert resource.disconnect_codes == [], _ASSERTION_FAILURE
    assert not resource.has_router_session, _ASSERTION_FAILURE


@pytest.mark.asyncio
async def test_pre_approval_setup_failure_releases_router_session() -> None:
    """A failed setup leaves no router-owned session on the resource."""
    error = RuntimeError("connect failed")

    class FailingResource(RecordingResource):
        async def on_connect(
            self, req: falcon.Request, ws: object, **params: object
        ) -> bool:
            del req, ws, params
            raise error

    router = make_session_router(FailingResource)
    socket = SessionSocket()

    with pytest.raises(RuntimeError) as raised:
        await router.on_websocket(
            typ.cast("typ.Any", make_session_request()), typ.cast("typ.Any", socket)
        )

    resource = FailingResource.instances[0]
    assert raised.value is error, _ASSERTION_FAILURE
    assert resource.disconnect_codes == [], _ASSERTION_FAILURE
    assert not resource.has_router_session, _ASSERTION_FAILURE


@pytest.mark.asyncio
async def test_clean_client_close_runs_disconnect_once_with_received_code(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A client close is reported once with Falcon's disconnect code."""
    caplog.set_level(logging.DEBUG, logger="falcon_pachinko._session_driver")
    router = make_session_router()
    socket = SessionSocket(falcon.WebSocketDisconnected(4000))

    await router.on_websocket(
        typ.cast("typ.Any", make_session_request()), typ.cast("typ.Any", socket)
    )

    assert len(RecordingResource.instances[0].disconnect_codes) == 1, _ASSERTION_FAILURE
    assert RecordingResource.instances[0].disconnect_codes == [4000], _ASSERTION_FAILURE
    observations = [
        record
        for record in caplog.records
        if record.name == "falcon_pachinko._session_driver"
    ]
    assert getattr(observations[-1], "outcome", None) == "completed", _ASSERTION_FAILURE
    assert getattr(observations[-1], "phase", None) == "closed", _ASSERTION_FAILURE
    assert getattr(observations[-1], "close_code", None) == 4000, _ASSERTION_FAILURE
    assert getattr(observations[-1], "duration_seconds", -1) >= 0, _ASSERTION_FAILURE


@pytest.mark.asyncio
async def test_missing_disconnect_code_uses_abnormal_closure(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A transport that omits its code reports abnormal closure to cleanup."""
    caplog.set_level(logging.DEBUG, logger="falcon_pachinko._session_driver")
    disconnect = falcon.WebSocketDisconnected(4000)
    del disconnect.code
    router = make_session_router()
    socket = SessionSocket(disconnect)

    await router.on_websocket(
        typ.cast("typ.Any", make_session_request()), typ.cast("typ.Any", socket)
    )

    assert RecordingResource.instances[0].disconnect_codes == [1006], _ASSERTION_FAILURE
    observations = [
        record
        for record in caplog.records
        if record.name == "falcon_pachinko._session_driver"
        and getattr(record, "operation", None) == "session"
    ]
    assert getattr(observations[-1], "close_code", None) == 1006, _ASSERTION_FAILURE


@given(actions=st.lists(st.sampled_from(_SESSION_ACTIONS), max_size=40))
def test_generated_session_actions_preserve_phase_and_receive_guards(
    actions: list[str],
) -> None:
    """Generated transitions preserve exactly-once ownership and receive state."""
    session = SessionState()
    expected_phase = SessionPhase.CONNECTING

    for action in actions:
        match action:
            case "establish":
                session.establish()
                if expected_phase is SessionPhase.CONNECTING:
                    expected_phase = SessionPhase.ESTABLISHED
            case "disconnect":
                should_own_cleanup = expected_phase is SessionPhase.ESTABLISHED
                assert session.begin_disconnect() is should_own_cleanup, (
                    _ASSERTION_FAILURE
                )
                if should_own_cleanup:
                    expected_phase = SessionPhase.DISCONNECTING
            case "close":
                session.mark_closed()
                expected_phase = SessionPhase.CLOSED
            case _:
                assert session.can_receive is (
                    expected_phase is SessionPhase.ESTABLISHED
                ), _ASSERTION_FAILURE

        assert session.phase is expected_phase, _ASSERTION_FAILURE
        assert session.can_receive is (expected_phase is SessionPhase.ESTABLISHED), (
            _ASSERTION_FAILURE
        )


def test_has_router_session_covers_setup_and_cleanup_phases() -> None:
    """Expose session ownership while setup still needs to observe acceptance."""
    resource = WebSocketResource()
    assert not resource.has_router_session, _ASSERTION_FAILURE

    manager = HookManager(global_hooks=HookCollection(), resources=(resource,))
    resource.bind_hook_manager(manager)
    assert not resource.has_router_session, _ASSERTION_FAILURE

    session = SessionState()
    manager.bind_session(session)
    assert resource.has_router_session, _ASSERTION_FAILURE
    assert not session.has_established, _ASSERTION_FAILURE
    session.establish()
    assert resource.has_router_session, _ASSERTION_FAILURE
    assert session.has_established, _ASSERTION_FAILURE
    assert session.begin_disconnect(), _ASSERTION_FAILURE
    assert resource.has_router_session, _ASSERTION_FAILURE
    session.mark_closed()
    assert not resource.has_router_session, _ASSERTION_FAILURE
    assert session.has_established, _ASSERTION_FAILURE


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["on_connect", "handler"])
async def test_resource_observed_disconnect_retains_transport_code(stage: str) -> None:
    """A disconnect raised during resource work is a transport ending."""

    class DisconnectedResource(RecordingResource):
        async def on_connect(
            self, req: falcon.Request, ws: object, **params: object
        ) -> bool:
            del req, params
            socket = typ.cast("SessionSocket", ws)
            await socket.accept()
            if stage == "on_connect":
                await socket.close(4005)
                raise falcon.WebSocketDisconnected(4005)
            return True

        async def on_unhandled(self, ws: object, message: str | bytes) -> None:
            del message
            await typ.cast("SessionSocket", ws).close(4005)
            raise falcon.WebSocketDisconnected(4005)

    router = make_session_router(DisconnectedResource)
    socket = SessionSocket(b'{"type":"close"}')

    await router.on_websocket(
        typ.cast("typ.Any", make_session_request()), typ.cast("typ.Any", socket)
    )

    assert DisconnectedResource.instances[0].disconnect_codes == [4005], (
        "resource-observed disconnects must preserve their transport code"
    )


@pytest.mark.asyncio
async def test_accepted_on_connect_failure_runs_cleanup_and_preserves_failure() -> None:
    """Acceptance inside on_connect establishes cleanup even when it raises."""
    primary = ValueError("setup failed")

    class AcceptThenFail(RecordingResource):
        async def on_connect(
            self, req: falcon.Request, ws: object, **params: object
        ) -> bool:
            del req, params
            await typ.cast("SessionSocket", ws).accept()
            raise primary

    router = make_session_router(AcceptThenFail)
    socket = SessionSocket()

    with pytest.raises(ValueError, match="setup failed") as error:
        await router.on_websocket(
            typ.cast("typ.Any", make_session_request()), typ.cast("typ.Any", socket)
        )

    assert error.value is primary, _ASSERTION_FAILURE
    assert AcceptThenFail.instances[0].disconnect_codes == [1011], _ASSERTION_FAILURE
    assert socket.close_codes == [1011], _ASSERTION_FAILURE


@pytest.mark.asyncio
async def test_accept_failure_after_approval_runs_disconnect_cleanup() -> None:
    """An accept error after approval still closes the promised session."""

    class AcceptFailureSocket(SessionSocket):
        async def accept(self, subprotocol: str | None = None) -> None:
            await super().accept(subprotocol)
            raise OSError("failed")

    router = make_session_router()
    socket = AcceptFailureSocket()

    with pytest.raises(OSError, match="failed"):
        await router.on_websocket(
            typ.cast("typ.Any", make_session_request()), typ.cast("typ.Any", socket)
        )

    assert RecordingResource.instances[0].disconnect_codes == [1011], _ASSERTION_FAILURE
    assert socket.close_codes == [1011], _ASSERTION_FAILURE


@pytest.mark.asyncio
async def test_after_connect_failure_after_approval_runs_cleanup() -> None:
    """Approval establishes cleanup before after-connect hooks run."""
    error = RuntimeError("after-connect failed")
    router = make_session_router()

    def fail_after_connect(context: object) -> None:
        del context
        raise error

    router.global_hooks.add(HookEvent.AFTER_CONNECT, fail_after_connect)
    socket = SessionSocket()

    with pytest.raises(RuntimeError) as raised:
        await router.on_websocket(
            typ.cast("typ.Any", make_session_request()), typ.cast("typ.Any", socket)
        )

    assert raised.value is error, _ASSERTION_FAILURE
    assert RecordingResource.instances[0].disconnect_codes == [1011], _ASSERTION_FAILURE
    assert socket.close_codes == [1011], _ASSERTION_FAILURE


@pytest.mark.asyncio
async def test_approval_without_acceptance_skips_receive_and_cleans_up(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Approval promises cleanup even if a transport never becomes accepted."""
    caplog.set_level(logging.DEBUG, logger="falcon_pachinko._session_driver")

    class UnacceptedSocket(SessionSocket):
        async def accept(self, subprotocol: str | None = None) -> None:
            del subprotocol

    router = make_session_router()
    socket = UnacceptedSocket(b'{"type":"unaccepted"}')
    await router.on_websocket(
        typ.cast("typ.Any", make_session_request()), typ.cast("typ.Any", socket)
    )

    assert not socket.receive_started.is_set(), "unaccepted sockets cannot receive"
    assert RecordingResource.instances[0].disconnect_codes == [1011], (
        "approval must still promise cleanup with the setup-failure code"
    )
    assert not any(
        record.name == "falcon_pachinko._session_driver"
        and getattr(record, "operation", None) == "acceptance"
        and getattr(record, "outcome", None) == "completed"
        for record in caplog.records
    ), "approval alone must not be logged as observed transport acceptance"


@pytest.mark.asyncio
async def test_resource_acceptance_with_falsey_result_reads_close_code() -> None:
    """Resource-owned acceptance establishes cleanup even when it rejects."""

    class AcceptAndReject(RecordingResource):
        async def on_connect(
            self, req: falcon.Request, ws: object, **params: object
        ) -> bool:
            del req, params
            await typ.cast("SessionSocket", ws).accept()
            return False

    router = make_session_router(AcceptAndReject)
    socket = SessionSocket()

    await router.on_websocket(
        typ.cast("typ.Any", make_session_request()), typ.cast("typ.Any", socket)
    )

    assert socket.close_codes == [1000], _ASSERTION_FAILURE
    assert AcceptAndReject.instances[0].disconnect_codes == [1000], _ASSERTION_FAILURE
