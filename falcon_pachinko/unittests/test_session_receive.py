"""Focused router-owned session receive contracts."""

from __future__ import annotations

import typing as typ

import falcon
import pytest

from falcon_pachinko.hooks import HookEvent
from falcon_pachinko.resource import WebSocketResource
from falcon_pachinko.router import WebSocketRouter
from falcon_pachinko.unittests._session_fixtures import (
    RecordingResource,
    SessionSocket,
    make_session_request,
    make_session_router,
)

_ASSERTION_FAILURE = "lifecycle expectation failed"


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
    router = WebSocketRouter()
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

    assert RecordingResource.instances[0].disconnect_codes == [4000], _ASSERTION_FAILURE
