"""Focused router-owned session receive contracts."""

from __future__ import annotations

import asyncio
import typing as typ

import falcon
import pytest

from falcon_pachinko._session import SessionState
from falcon_pachinko._session_socket import SessionWebSocket, receive_frame
from falcon_pachinko.hooks import HookCollection, HookEvent, HookManager
from falcon_pachinko.resource import WebSocketResource
from falcon_pachinko.router import WebSocketRouter
from falcon_pachinko.unittests._session_fixtures import (
    RecordingResource,
    SessionSocket,
    make_session_request,
    make_session_router,
)
from falcon_pachinko.unittests.helpers import DummyWS

_ASSERTION_FAILURE = "lifecycle expectation failed"


@pytest.mark.asyncio
async def test_mapping_media_is_preserved_as_one_json_frame() -> None:
    """A mapping frame is media, not a batch of frames to iterate."""
    websocket = DummyWS({"type": "ping"})

    assert await receive_frame(websocket) == b'{"type":"ping"}', (
        "mapping media must be encoded as one JSON frame"
    )


@pytest.mark.asyncio
async def test_close_before_buffered_frame_skips_overridden_dispatch() -> None:
    """A closed transport cannot enter even an overridden resource dispatcher."""

    class AcceptAndClose(RecordingResource):
        async def on_connect(
            self, req: falcon.Request, ws: object, **params: object
        ) -> bool:
            del req, params
            socket = typ.cast("SessionSocket", ws)
            await socket.accept()
            await socket.close(4004)
            return True

    router = make_session_router(AcceptAndClose)
    socket = SessionSocket(b'{"type":"buffered"}')

    await router.on_websocket(
        typ.cast("typ.Any", make_session_request()), typ.cast("typ.Any", socket)
    )

    resource = AcceptAndClose.instances[0]
    assert resource.raw_frames == [], "a closed transport must not dispatch frames"
    assert resource.disconnect_codes == [4004], (
        "discarding late frames must preserve the reported disconnect code"
    )


@pytest.mark.asyncio
async def test_closed_session_does_not_dispatch_or_emit_receive_hooks() -> None:
    """A resource retained after closure cannot start another receive cycle."""
    router = make_session_router()
    receive_events: list[str] = []
    router.global_hooks.add(
        HookEvent.BEFORE_RECEIVE,
        lambda context: receive_events.append(str(context.event)),
    )
    socket = SessionSocket(falcon.WebSocketDisconnected(4000))

    await router.on_websocket(
        typ.cast("typ.Any", make_session_request()), typ.cast("typ.Any", socket)
    )
    resource = RecordingResource.instances[0]
    await WebSocketResource.dispatch(
        resource, typ.cast("typ.Any", socket), b'{"type":"late"}'
    )

    assert not receive_events, _ASSERTION_FAILURE
    assert resource.raw_frames == [], _ASSERTION_FAILURE


@pytest.mark.asyncio
async def test_close_during_receive_skips_later_receive_hooks() -> None:
    """A close from a receive hook prevents dispatch and its after hook."""
    router = make_session_router()
    events: list[str] = []

    async def close_before_receive(context: object) -> None:
        events.append("before")
        typed_context = typ.cast("typ.Any", context)
        await typed_context.ws.close(4001)

    router.global_hooks.add(HookEvent.BEFORE_RECEIVE, close_before_receive)
    router.global_hooks.add(
        HookEvent.AFTER_RECEIVE, lambda _context: events.append("after")
    )
    socket = SessionSocket(b'{"type":"too-late"}')

    await router.on_websocket(
        typ.cast("typ.Any", make_session_request()), typ.cast("typ.Any", socket)
    )

    assert events == ["before"], "a closed socket must not emit after-receive"
    assert RecordingResource.instances[0].handled_frames == [], (
        "a close from before-receive must suppress message handling"
    )


@pytest.mark.asyncio
async def test_close_guard_overrides_wrapped_is_closed_probe() -> None:
    """A pending transport close blocks receive hooks before it completes."""

    class ClosePendingSocket(SessionSocket):
        def __init__(self) -> None:
            super().__init__(b'{"type":"too-late"}')
            self.close_started = asyncio.Event()
            self.finish_close = asyncio.Event()

        @property
        def is_closed(self) -> bool:
            return self.closed

        async def close(self, code: int = 1000) -> None:
            self.close_started.set()
            await self.finish_close.wait()
            await super().close(code)

    session = SessionState()
    session.establish()
    resource = RecordingResource()
    hook_manager = HookManager(
        global_hooks=HookCollection(),
        resources=[resource],
    )
    hook_manager.bind_session(session)
    socket = ClosePendingSocket()
    websocket = SessionWebSocket(typ.cast("typ.Any", socket), session)
    close_task = asyncio.create_task(websocket.close(4001))

    await asyncio.wait_for(socket.close_started.wait(), timeout=1)
    transport_still_open = not socket.is_closed
    receive_still_allowed = hook_manager.can_receive(typ.cast("typ.Any", websocket))
    socket.finish_close.set()
    await asyncio.wait_for(close_task, timeout=1)

    assert transport_still_open, "the transport close must still be pending"
    assert not receive_still_allowed, (
        "receive eligibility must stop when the wrapper begins closing"
    )
    assert session.close_code == 4001, "cleanup must retain the sent close code"


@pytest.mark.asyncio
async def test_handler_close_skips_after_receive_hooks() -> None:
    """A handler close prevents after-receive hooks from starting."""
    events: list[str] = []

    class ClosingResource(RecordingResource):
        async def on_unhandled(self, ws: object, message: str | bytes) -> None:
            await super().on_unhandled(ws, message)
            await typ.cast("typ.Any", ws).close(4004)

    router = make_session_router(ClosingResource)
    router.global_hooks.add(
        HookEvent.BEFORE_RECEIVE, lambda _context: events.append("before")
    )
    router.global_hooks.add(
        HookEvent.AFTER_RECEIVE, lambda _context: events.append("after")
    )
    socket = SessionSocket(b'{"type":"unhandled"}')

    await router.on_websocket(
        typ.cast("typ.Any", make_session_request()), typ.cast("typ.Any", socket)
    )

    assert events == ["before"], "after-receive must not run after handler close"
    assert ClosingResource.instances[0].disconnect_codes == [4004], (
        "handler closure must still run disconnect cleanup"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("event", [HookEvent.BEFORE_RECEIVE, HookEvent.AFTER_RECEIVE])
async def test_close_from_receive_hook_stops_remaining_hook_layers(
    event: HookEvent,
) -> None:
    """Closing in a hook stops the remaining hooks in every receive layer."""
    events: list[str] = []

    class HookedResource(RecordingResource):
        pass

    async def close_socket(context: object) -> None:
        events.append("close")
        await typ.cast("typ.Any", context).ws.close(4001)

    def record_late_hook(context: object) -> None:
        del context
        events.append("late")

    router = make_session_router(HookedResource)
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
    socket = SessionSocket(b'{"type":"message"}')

    await router.on_websocket(
        typ.cast("typ.Any", make_session_request()), typ.cast("typ.Any", socket)
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
    router = make_session_router()
    socket = SessionSocket(
        text, binary, {"type": "object"}, falcon.WebSocketDisconnected(4000)
    )

    await router.on_websocket(
        typ.cast("typ.Any", make_session_request()), typ.cast("typ.Any", socket)
    )

    assert RecordingResource.instances[0].raw_frames == [
        text,
        binary,
        b'{"type":"object"}',
    ], _ASSERTION_FAILURE


@pytest.mark.asyncio
async def test_path_converter_mount_and_uri_parameter_are_accepted() -> None:
    """Falcon's catch-all descendant route preserves the mounted router path."""
    RecordingResource.instances.clear()
    router = WebSocketRouter(receive_loop=True)
    router.add_route("/rooms/{room}", RecordingResource)
    router.mount("/ws")
    socket = SessionSocket(falcon.WebSocketDisconnected(4000))

    await router.on_websocket(
        typ.cast(
            "typ.Any",
            make_session_request("/ws/rooms/blue", "/ws/{pachinko_path:path}"),
        ),
        typ.cast("typ.Any", socket),
        pachinko_path="rooms/blue",
    )

    assert len(RecordingResource.instances) == 1, _ASSERTION_FAILURE
    assert RecordingResource.instances[-1].disconnect_codes == [4000], (
        _ASSERTION_FAILURE
    )
