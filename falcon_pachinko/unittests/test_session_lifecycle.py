"""Focused router-owned session lifecycle contracts."""

from __future__ import annotations

import typing as typ

import falcon
import pytest

if typ.TYPE_CHECKING:
    import collections.abc as cabc

from falcon_pachinko.hooks import HookEvent
from falcon_pachinko.lifecycle import LifecycleError
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
        async def accept(
            self,
            subprotocol: str | None = None,
            headers: cabc.Mapping[str, str]
            | cabc.Iterable[tuple[str, str]]
            | None = None,
        ) -> None:
            await super().accept(subprotocol, headers)
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
async def test_after_connect_failure_after_approval_rejects_before_acceptance() -> None:
    """A failing post-connect hook still runs before router acceptance."""
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
    assert socket.unaccepted, _ASSERTION_FAILURE
    assert RecordingResource.instances[0].disconnect_codes == [], _ASSERTION_FAILURE
    assert socket.close_codes == [1000], _ASSERTION_FAILURE


@pytest.mark.asyncio
async def test_noop_transport_acceptance_never_starts_a_session() -> None:
    """Reject transports whose accept call returns without accepting."""

    class UnacceptedSocket(SessionSocket):
        async def accept(
            self,
            subprotocol: str | None = None,
            headers: cabc.Mapping[str, str]
            | cabc.Iterable[tuple[str, str]]
            | None = None,
        ) -> None:
            del subprotocol, headers

    router = make_session_router()
    socket = UnacceptedSocket(b'{"type":"unaccepted"}')
    with pytest.raises(LifecycleError, match="without accepting"):
        await router.on_websocket(
            typ.cast("typ.Any", make_session_request()), typ.cast("typ.Any", socket)
        )

    assert not socket.receive_started.is_set(), "unaccepted sockets cannot receive"
    assert RecordingResource.instances[0].disconnect_codes == [], (
        "a failed acceptance cannot start disconnect cleanup"
    )
    assert socket.close_codes == [1000], "failed acceptance must close the transport"


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
