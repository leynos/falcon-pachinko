"""Focused contracts for the router-owned WebSocket session lifecycle."""

from __future__ import annotations

import asyncio
import collections as cl
import types
import typing as typ

import falcon
import pytest

from falcon_pachinko.hooks import HookEvent
from falcon_pachinko.resource import WebSocketResource
from falcon_pachinko.router import WebSocketRouter

_ASSERTION_FAILURE = "lifecycle expectation failed"


class _SessionSocket:
    """Small Falcon-shaped socket with deterministic receive outcomes."""

    def __init__(
        self,
        *frames: object,
        block_receive: bool = False,
    ) -> None:
        self.unaccepted = True
        self.closed = False
        self.accepted = asyncio.Event()
        self.receive_started = asyncio.Event()
        self._frames: cl.deque[object] = cl.deque(frames)
        self._block_receive = block_receive
        self._release_receive = asyncio.Event()
        self.close_codes: list[int] = []

    async def accept(self, subprotocol: str | None = None) -> None:
        del subprotocol
        self.unaccepted = False
        self.accepted.set()

    async def close(self, code: int = 1000) -> None:
        if self.closed:
            return
        self.closed = True
        self.close_codes.append(code)

    async def send_media(self, data: object) -> None:
        del data

    async def receive_media(self) -> object:
        self.receive_started.set()
        if self._block_receive:
            await self._release_receive.wait()
        if self._frames:
            frame = self._frames.popleft()
            if isinstance(frame, BaseException):
                raise frame
            return frame
        raise falcon.WebSocketDisconnected(self.close_codes[0] if self.closed else 1000)


def _request(path: str = "/session", path_template: str = "/") -> object:
    return types.SimpleNamespace(path=path, path_template=path_template)


class _RecordingResource(WebSocketResource):
    instances: typ.ClassVar[list[_RecordingResource]] = []

    def __init__(self, *, accepted: bool = True) -> None:
        self.accept_on_connect = accepted
        self.disconnect_codes: list[int] = []
        self.disconnect_started = asyncio.Event()
        self.finish_disconnect = asyncio.Event()
        self.wait_in_disconnect = False
        self.raw_frames: list[str | bytes] = []
        self.handled_frames: list[str | bytes] = []
        self.dispatch_error: Exception | None = None
        type(self).instances.append(self)

    async def on_connect(
        self, req: falcon.Request, ws: object, **params: object
    ) -> bool:
        del req, ws, params
        return self.accept_on_connect

    async def on_disconnect(self, ws: object, close_code: int) -> None:
        del ws
        self.disconnect_codes.append(close_code)
        self.disconnect_started.set()
        if self.wait_in_disconnect:
            await self.finish_disconnect.wait()

    async def dispatch(self, ws: object, raw: str | bytes) -> None:
        self.raw_frames.append(raw)
        if self.dispatch_error is not None:
            raise self.dispatch_error
        await super().dispatch(typ.cast("typ.Any", ws), raw)

    async def on_unhandled(self, ws: object, message: str | bytes) -> None:
        del ws
        self.handled_frames.append(message)


def _router(resource: type[WebSocketResource] = _RecordingResource) -> WebSocketRouter:
    _RecordingResource.instances.clear()
    router = WebSocketRouter()
    router.add_route("/session", resource)
    router.mount("/")
    return router


@pytest.mark.asyncio
async def test_rejected_connection_closes_without_disconnect_cleanup() -> None:
    """A falsey decision before acceptance rejects without post-accept cleanup."""
    socket = _SessionSocket()

    class RejectingResource(_RecordingResource):
        def __init__(self) -> None:
            super().__init__(accepted=False)

    router = _router(RejectingResource)
    await router.on_websocket(
        typ.cast("typ.Any", _request()), typ.cast("typ.Any", socket)
    )

    assert socket.unaccepted is True, _ASSERTION_FAILURE
    assert socket.close_codes == [1000], _ASSERTION_FAILURE
    assert RejectingResource.instances[0].disconnect_codes == [], _ASSERTION_FAILURE


@pytest.mark.asyncio
async def test_clean_client_close_runs_disconnect_once_with_received_code() -> None:
    """A client close is reported once with Falcon's disconnect code."""
    router = _router()
    socket = _SessionSocket(falcon.WebSocketDisconnected(4000))

    await router.on_websocket(
        typ.cast("typ.Any", _request()), typ.cast("typ.Any", socket)
    )

    assert len(_RecordingResource.instances[0].disconnect_codes) == 1, (
        _ASSERTION_FAILURE
    )
    assert _RecordingResource.instances[0].disconnect_codes == [4000], (
        _ASSERTION_FAILURE
    )


@pytest.mark.asyncio
async def test_missing_disconnect_code_uses_abnormal_closure() -> None:
    """A transport that omits its code reports abnormal closure to cleanup."""
    disconnect = falcon.WebSocketDisconnected(4000)
    del disconnect.code
    router = _router()
    socket = _SessionSocket(disconnect)

    await router.on_websocket(
        typ.cast("typ.Any", _request()), typ.cast("typ.Any", socket)
    )

    assert _RecordingResource.instances[0].disconnect_codes == [1006], (
        _ASSERTION_FAILURE
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["on_connect", "handler"])
async def test_resource_observed_disconnect_retains_transport_code(stage: str) -> None:
    """A disconnect raised during resource work is a transport ending."""

    class DisconnectedResource(_RecordingResource):
        async def on_connect(
            self, req: falcon.Request, ws: object, **params: object
        ) -> bool:
            del req, params
            socket = typ.cast("_SessionSocket", ws)
            await socket.accept()
            if stage == "on_connect":
                await socket.close(4005)
                raise falcon.WebSocketDisconnected(4005)
            return True

        async def on_unhandled(self, ws: object, message: str | bytes) -> None:
            del message
            await typ.cast("_SessionSocket", ws).close(4005)
            raise falcon.WebSocketDisconnected(4005)

    router = _router(DisconnectedResource)
    socket = _SessionSocket(b'{"type":"close"}')

    await router.on_websocket(
        typ.cast("typ.Any", _request()), typ.cast("typ.Any", socket)
    )

    assert DisconnectedResource.instances[0].disconnect_codes == [4005], (
        "resource-observed disconnects must preserve their transport code"
    )


@pytest.mark.asyncio
async def test_accepted_on_connect_failure_runs_cleanup_and_preserves_failure() -> None:
    """Acceptance inside on_connect establishes cleanup even when it raises."""
    primary = ValueError("setup failed")

    class AcceptThenFail(_RecordingResource):
        async def on_connect(
            self, req: falcon.Request, ws: object, **params: object
        ) -> bool:
            del req, params
            await typ.cast("_SessionSocket", ws).accept()
            raise primary

    router = _router(AcceptThenFail)
    socket = _SessionSocket()

    with pytest.raises(ValueError, match="setup failed") as error:
        await router.on_websocket(
            typ.cast("typ.Any", _request()), typ.cast("typ.Any", socket)
        )

    assert error.value is primary, _ASSERTION_FAILURE
    assert AcceptThenFail.instances[0].disconnect_codes == [1011], _ASSERTION_FAILURE
    assert socket.close_codes == [1011], _ASSERTION_FAILURE


@pytest.mark.asyncio
async def test_accepted_on_connect_failure_and_after_hook_failure_are_grouped() -> None:
    """An after-connect error cannot hide the original accepted setup failure."""
    primary = ValueError("setup failed")
    hook_error = RuntimeError("after-connect failed")

    class AcceptThenFail(_RecordingResource):
        async def on_connect(
            self, req: falcon.Request, ws: object, **params: object
        ) -> bool:
            del req, params
            await typ.cast("_SessionSocket", ws).accept()
            raise primary

    def fail_after_connect(context: object) -> None:
        del context
        raise hook_error

    router = _router(AcceptThenFail)
    router.global_hooks.add(HookEvent.AFTER_CONNECT, fail_after_connect)
    socket = _SessionSocket()

    with pytest.raises(ExceptionGroup, match="session") as raised:
        await router.on_websocket(
            typ.cast("typ.Any", _request()), typ.cast("typ.Any", socket)
        )

    assert raised.value.exceptions == (primary, hook_error), (
        "setup failures must remain visible before the after-connect failure"
    )
    assert AcceptThenFail.instances[0].disconnect_codes == [1011], (
        "failed setup must still execute the promised cleanup"
    )


@pytest.mark.asyncio
async def test_after_connect_cancellation_reports_original_setup_failure() -> None:
    """Cancellation stays bare while the original setup failure is reported."""
    primary = ValueError("setup failed")
    cancellation = asyncio.CancelledError("after-connect cancelled")

    class AcceptThenFail(_RecordingResource):
        async def on_connect(
            self, req: falcon.Request, ws: object, **params: object
        ) -> bool:
            del req, params
            await typ.cast("_SessionSocket", ws).accept()
            raise primary

    def cancel_after_connect(context: object) -> None:
        del context
        raise cancellation

    router = _router(AcceptThenFail)
    router.global_hooks.add(HookEvent.AFTER_CONNECT, cancel_after_connect)
    socket = _SessionSocket()
    loop = asyncio.get_running_loop()
    reported: list[object] = []
    previous_handler = loop.get_exception_handler()
    loop.set_exception_handler(
        lambda _loop, context: reported.append(context.get("exception"))
    )
    try:
        with pytest.raises(asyncio.CancelledError) as raised:
            await router.on_websocket(
                typ.cast("typ.Any", _request()), typ.cast("typ.Any", socket)
            )
    finally:
        loop.set_exception_handler(previous_handler)

    assert raised.value is cancellation, "the original cancellation must stay bare"
    assert reported == [primary], "the original setup failure must stay observable"
    assert AcceptThenFail.instances[0].disconnect_codes == [1001], (
        "setup cancellation must still execute the promised cleanup"
    )


@pytest.mark.asyncio
async def test_accept_failure_after_approval_runs_disconnect_cleanup() -> None:
    """An accept error after approval still closes the promised session."""

    class AcceptFailureSocket(_SessionSocket):
        async def accept(self, subprotocol: str | None = None) -> None:
            await super().accept(subprotocol)
            raise OSError("failed")

    router = _router()
    socket = AcceptFailureSocket()

    with pytest.raises(OSError, match="failed"):
        await router.on_websocket(
            typ.cast("typ.Any", _request()), typ.cast("typ.Any", socket)
        )

    assert _RecordingResource.instances[0].disconnect_codes == [1011], (
        _ASSERTION_FAILURE
    )
    assert socket.close_codes == [1011], _ASSERTION_FAILURE


@pytest.mark.asyncio
async def test_after_connect_failure_after_approval_runs_cleanup() -> None:
    """Approval establishes cleanup before after-connect hooks run."""
    error = RuntimeError("after-connect failed")
    router = _router()

    def fail_after_connect(context: object) -> None:
        del context
        raise error

    router.global_hooks.add(HookEvent.AFTER_CONNECT, fail_after_connect)
    socket = _SessionSocket()

    with pytest.raises(RuntimeError) as raised:
        await router.on_websocket(
            typ.cast("typ.Any", _request()), typ.cast("typ.Any", socket)
        )

    assert raised.value is error, _ASSERTION_FAILURE
    assert _RecordingResource.instances[0].disconnect_codes == [1011], (
        _ASSERTION_FAILURE
    )
    assert socket.close_codes == [1011], _ASSERTION_FAILURE


@pytest.mark.asyncio
@pytest.mark.parametrize("expose_state", [True, False])
async def test_approval_without_acceptance_skips_receive_and_cleans_up(
    *,
    expose_state: bool,
) -> None:
    """Approval promises cleanup even if a transport never becomes accepted."""

    class UnacceptedSocket(_SessionSocket):
        async def accept(self, subprotocol: str | None = None) -> None:
            del subprotocol

    router = _router()
    socket = UnacceptedSocket(b'{"type":"unaccepted"}')
    if not expose_state:
        del socket.unaccepted

    await router.on_websocket(
        typ.cast("typ.Any", _request()), typ.cast("typ.Any", socket)
    )

    assert not socket.receive_started.is_set(), "unaccepted sockets cannot receive"
    assert _RecordingResource.instances[0].disconnect_codes == [1011], (
        "approval must still promise cleanup with the setup-failure code"
    )


@pytest.mark.asyncio
async def test_resource_acceptance_with_falsey_result_reads_close_code() -> None:
    """Resource-owned acceptance establishes cleanup even when it rejects."""

    class AcceptAndReject(_RecordingResource):
        async def on_connect(
            self, req: falcon.Request, ws: object, **params: object
        ) -> bool:
            del req, params
            await typ.cast("_SessionSocket", ws).accept()
            return False

    router = _router(AcceptAndReject)
    socket = _SessionSocket()

    await router.on_websocket(
        typ.cast("typ.Any", _request()), typ.cast("typ.Any", socket)
    )

    assert socket.close_codes == [1000], _ASSERTION_FAILURE
    assert AcceptAndReject.instances[0].disconnect_codes == [1000], _ASSERTION_FAILURE


@pytest.mark.asyncio
async def test_close_before_buffered_frame_skips_overridden_dispatch() -> None:
    """A closed transport cannot enter even an overridden resource dispatcher."""

    class AcceptAndClose(_RecordingResource):
        async def on_connect(
            self, req: falcon.Request, ws: object, **params: object
        ) -> bool:
            del req, params
            socket = typ.cast("_SessionSocket", ws)
            await socket.accept()
            await socket.close(4004)
            return True

    router = _router(AcceptAndClose)
    socket = _SessionSocket(b'{"type":"buffered"}')

    await router.on_websocket(
        typ.cast("typ.Any", _request()), typ.cast("typ.Any", socket)
    )

    resource = AcceptAndClose.instances[0]
    assert resource.raw_frames == [], "a closed transport must not dispatch frames"
    assert resource.disconnect_codes == [4004], (
        "discarding late frames must preserve the reported disconnect code"
    )


@pytest.mark.asyncio
async def test_session_and_disconnect_failures_are_grouped_primary_first() -> None:
    """Disconnect failures are visible after the original handler failure."""
    primary = RuntimeError("handler failed")
    cleanup = LookupError("disconnect failed")

    class FailingResource(_RecordingResource):
        async def dispatch(self, ws: object, raw: str | bytes) -> None:
            del ws, raw
            raise primary

        async def on_disconnect(self, ws: object, close_code: int) -> None:
            await super().on_disconnect(ws, close_code)
            raise cleanup

    router = _router(FailingResource)
    socket = _SessionSocket(b'{"type":"boom"}')

    with pytest.raises(ExceptionGroup) as error:
        await router.on_websocket(
            typ.cast("typ.Any", _request()), typ.cast("typ.Any", socket)
        )

    assert error.value.exceptions == (primary, cleanup), _ASSERTION_FAILURE
    assert FailingResource.instances[0].disconnect_codes == [1011], _ASSERTION_FAILURE
    assert socket.close_codes == [1011], _ASSERTION_FAILURE


@pytest.mark.asyncio
async def test_cleanup_only_failure_is_propagated_unchanged() -> None:
    """A lone resource cleanup failure remains the original exception."""
    cleanup = LookupError("disconnect failed")

    class FailingCleanupResource(_RecordingResource):
        async def on_disconnect(self, ws: object, close_code: int) -> None:
            await super().on_disconnect(ws, close_code)
            raise cleanup

    router = _router(FailingCleanupResource)
    socket = _SessionSocket(falcon.WebSocketDisconnected(4000))

    with pytest.raises(LookupError) as error:
        await router.on_websocket(
            typ.cast("typ.Any", _request()), typ.cast("typ.Any", socket)
        )

    assert error.value is cleanup, _ASSERTION_FAILURE


@pytest.mark.asyncio
async def test_before_disconnect_failure_does_not_skip_resource_cleanup() -> None:
    """The resource hook runs even when a before-disconnect hook fails."""
    before_error = RuntimeError("before disconnect failed")
    resource_error = ValueError("resource cleanup failed")

    class FailingCleanupResource(_RecordingResource):
        async def on_disconnect(self, ws: object, close_code: int) -> None:
            await super().on_disconnect(ws, close_code)
            raise resource_error

    router = _router(FailingCleanupResource)

    def fail_before_disconnect(context: object) -> None:
        del context
        raise before_error

    router.global_hooks.add(HookEvent.BEFORE_DISCONNECT, fail_before_disconnect)
    socket = _SessionSocket(falcon.WebSocketDisconnected(4000))

    with pytest.raises(ExceptionGroup) as error:
        await router.on_websocket(
            typ.cast("typ.Any", _request()), typ.cast("typ.Any", socket)
        )

    assert error.value.exceptions == (before_error, resource_error), _ASSERTION_FAILURE
    assert FailingCleanupResource.instances[0].disconnect_codes == [4000], (
        _ASSERTION_FAILURE
    )


@pytest.mark.asyncio
async def test_before_disconnect_receives_code_and_primary_error() -> None:
    """Disconnect context exposes the effective code and original failure."""
    primary = RuntimeError("dispatch failed")
    context_values: list[tuple[int | None, BaseException | None]] = []

    def record_before_disconnect(context: object) -> None:
        typed_context = typ.cast("typ.Any", context)
        context_values.append((typed_context.close_code, typed_context.error))

    class FailingDispatchResource(_RecordingResource):
        async def dispatch(self, ws: object, raw: str | bytes) -> None:
            del ws, raw
            raise primary

    router = _router(FailingDispatchResource)
    router.global_hooks.add(HookEvent.BEFORE_DISCONNECT, record_before_disconnect)
    socket = _SessionSocket(b'{"type":"failed"}')

    with pytest.raises(RuntimeError) as await_error:
        await router.on_websocket(
            typ.cast("typ.Any", _request()), typ.cast("typ.Any", socket)
        )

    assert await_error.value is primary, _ASSERTION_FAILURE
    assert context_values == [(1011, primary)], _ASSERTION_FAILURE


@pytest.mark.asyncio
async def test_cancellation_during_receive_closes_and_runs_cleanup() -> None:
    """Cancellation closes with 1001 and is re-raised after cleanup."""
    router = _router()
    socket = _SessionSocket(block_receive=True)
    task = asyncio.create_task(
        router.on_websocket(
            typ.cast("typ.Any", _request()), typ.cast("typ.Any", socket)
        )
    )
    await socket.receive_started.wait()

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert socket.close_codes == [1001], _ASSERTION_FAILURE
    assert _RecordingResource.instances[0].disconnect_codes == [1001], (
        _ASSERTION_FAILURE
    )


@pytest.mark.asyncio
async def test_cancellation_reports_cleanup_failure_to_loop_handler() -> None:
    """Cleanup failures are observable without replacing cancellation."""
    cleanup = RuntimeError("disconnect failed")
    loop = asyncio.get_running_loop()
    reported: list[dict[str, object]] = []
    old_handler = loop.get_exception_handler()
    loop.set_exception_handler(lambda _loop, context: reported.append(context))

    class FailingCleanupResource(_RecordingResource):
        async def on_disconnect(self, ws: object, close_code: int) -> None:
            await super().on_disconnect(ws, close_code)
            raise cleanup

    router = _router(FailingCleanupResource)
    socket = _SessionSocket(block_receive=True)
    task = asyncio.create_task(
        router.on_websocket(
            typ.cast("typ.Any", _request()), typ.cast("typ.Any", socket)
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
    router = _router()
    socket = _SessionSocket(block_receive=True)
    task = asyncio.create_task(
        router.on_websocket(
            typ.cast("typ.Any", _request()), typ.cast("typ.Any", socket)
        )
    )
    await socket.receive_started.wait()
    resource = _RecordingResource.instances[0]
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

    class WaitingCleanupResource(_RecordingResource):
        def __init__(self) -> None:
            super().__init__()
            self.wait_in_disconnect = True

    router = _router(WaitingCleanupResource)
    socket = _SessionSocket(falcon.WebSocketDisconnected(4000))
    task = asyncio.create_task(
        router.on_websocket(
            typ.cast("typ.Any", _request()), typ.cast("typ.Any", socket)
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


@pytest.mark.asyncio
async def test_closed_session_does_not_dispatch_or_emit_receive_hooks() -> None:
    """A resource retained after closure cannot start another receive cycle."""
    router = _router()
    receive_events: list[str] = []
    router.global_hooks.add(
        HookEvent.BEFORE_RECEIVE,
        lambda context: receive_events.append(str(context.event)),
    )
    socket = _SessionSocket(falcon.WebSocketDisconnected(4000))

    await router.on_websocket(
        typ.cast("typ.Any", _request()), typ.cast("typ.Any", socket)
    )
    resource = _RecordingResource.instances[0]
    await WebSocketResource.dispatch(
        resource, typ.cast("typ.Any", socket), b'{"type":"late"}'
    )

    assert not receive_events, _ASSERTION_FAILURE
    assert resource.raw_frames == [], _ASSERTION_FAILURE


@pytest.mark.asyncio
async def test_close_during_receive_skips_later_receive_hooks() -> None:
    """A close from a receive hook prevents dispatch and its after hook."""
    router = _router()
    events: list[str] = []

    async def close_before_receive(context: object) -> None:
        events.append("before")
        typed_context = typ.cast("typ.Any", context)
        await typed_context.ws.close(4001)

    router.global_hooks.add(HookEvent.BEFORE_RECEIVE, close_before_receive)
    router.global_hooks.add(
        HookEvent.AFTER_RECEIVE, lambda _context: events.append("after")
    )
    socket = _SessionSocket(b'{"type":"too-late"}')

    await router.on_websocket(
        typ.cast("typ.Any", _request()), typ.cast("typ.Any", socket)
    )

    assert events == ["before"], "a closed socket must not emit after-receive"
    assert _RecordingResource.instances[0].handled_frames == [], (
        "a close from before-receive must suppress message handling"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("event", [HookEvent.BEFORE_RECEIVE, HookEvent.AFTER_RECEIVE])
async def test_close_from_receive_hook_stops_remaining_hook_layers(
    event: HookEvent,
) -> None:
    """Closing in a hook stops the remaining hooks in every receive layer."""
    events: list[str] = []

    class HookedResource(_RecordingResource):
        pass

    async def close_socket(context: object) -> None:
        events.append("close")
        await typ.cast("typ.Any", context).ws.close(4001)

    def record_late_hook(context: object) -> None:
        del context
        events.append("late")

    router = _router(HookedResource)
    first_layer = (
        router.global_hooks
        if event is HookEvent.BEFORE_RECEIVE
        else HookedResource.hooks
    )
    last_layer = (
        HookedResource.hooks
        if event is HookEvent.BEFORE_RECEIVE
        else router.global_hooks
    )
    first_layer.add(event, close_socket)
    first_layer.add(event, record_late_hook)
    last_layer.add(event, record_late_hook)
    socket = _SessionSocket(b'{"type":"message"}')

    await router.on_websocket(
        typ.cast("typ.Any", _request()), typ.cast("typ.Any", socket)
    )

    assert events == ["close"], "closing must prevent all later receive hooks"
    assert HookedResource.instances[0].disconnect_codes == [4001], (
        "hook closure must still run disconnect cleanup"
    )


@pytest.mark.asyncio
async def test_receive_adapter_preserves_frames_and_encodes_other_media() -> None:
    """Text and bytes stay raw while Falcon media values become JSON frames."""
    text = '{"type":"text"}'
    binary = b'{"type":"binary"}'
    router = _router()
    socket = _SessionSocket(
        text, binary, {"type": "object"}, falcon.WebSocketDisconnected(4000)
    )

    await router.on_websocket(
        typ.cast("typ.Any", _request()), typ.cast("typ.Any", socket)
    )

    assert _RecordingResource.instances[0].raw_frames == [
        text,
        binary,
        b'{"type":"object"}',
    ], _ASSERTION_FAILURE


@pytest.mark.asyncio
async def test_path_converter_mount_and_uri_parameter_are_accepted() -> None:
    """Falcon's catch-all descendant route preserves the mounted router path."""
    router = WebSocketRouter()
    router.add_route("/rooms/{room}", _RecordingResource)
    router.mount("/ws")
    socket = _SessionSocket(falcon.WebSocketDisconnected(4000))

    await router.on_websocket(
        typ.cast("typ.Any", _request("/ws/rooms/blue", "/ws/{pachinko_path:path}")),
        typ.cast("typ.Any", socket),
        pachinko_path="rooms/blue",
    )

    assert _RecordingResource.instances[0].disconnect_codes == [4000], (
        _ASSERTION_FAILURE
    )
