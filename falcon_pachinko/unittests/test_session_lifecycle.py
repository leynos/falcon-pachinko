"""Focused router-owned session lifecycle contracts."""

from __future__ import annotations

import typing as typ

import falcon
import pytest

from falcon_pachinko.hooks import HookEvent
from falcon_pachinko.unittests._session_fixtures import (
    RecordingResource,
    SessionSocket,
    make_session_request,
    make_session_router,
)

_ASSERTION_FAILURE = "lifecycle expectation failed"


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
    assert RejectingResource.instances[0].disconnect_codes == [], _ASSERTION_FAILURE


@pytest.mark.asyncio
async def test_clean_client_close_runs_disconnect_once_with_received_code() -> None:
    """A client close is reported once with Falcon's disconnect code."""
    router = make_session_router()
    socket = SessionSocket(falcon.WebSocketDisconnected(4000))

    await router.on_websocket(
        typ.cast("typ.Any", make_session_request()), typ.cast("typ.Any", socket)
    )

    assert len(RecordingResource.instances[0].disconnect_codes) == 1, _ASSERTION_FAILURE
    assert RecordingResource.instances[0].disconnect_codes == [4000], _ASSERTION_FAILURE


@pytest.mark.asyncio
async def test_missing_disconnect_code_uses_abnormal_closure() -> None:
    """A transport that omits its code reports abnormal closure to cleanup."""
    disconnect = falcon.WebSocketDisconnected(4000)
    del disconnect.code
    router = make_session_router()
    socket = SessionSocket(disconnect)

    await router.on_websocket(
        typ.cast("typ.Any", make_session_request()), typ.cast("typ.Any", socket)
    )

    assert RecordingResource.instances[0].disconnect_codes == [1006], _ASSERTION_FAILURE


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
async def test_approval_without_acceptance_skips_receive_and_cleans_up() -> None:
    """Approval promises cleanup even if a transport never becomes accepted."""

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
