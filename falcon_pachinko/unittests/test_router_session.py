"""Focused tests for Falcon WebSocket attachment and session handling."""

from __future__ import annotations

import asyncio
import typing as typ
import warnings

import falcon
import falcon.asgi
import falcon.media
import falcon.testing
import pytest
from hypothesis import example, given
from hypothesis import strategies as st

from falcon_pachinko import WebSocketResource, WebSocketRouter
from falcon_pachinko._disconnect import _PeerDisconnectedError
from falcon_pachinko.unittests.helpers import DummyWS, RecordingWS, make_req

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from falcon_pachinko.protocols import WebSocketLike


class PathResource(WebSocketResource):
    """Capture connection parameters for route matching tests."""

    instances: typ.ClassVar[list[PathResource]] = []

    async def on_connect(self, req: object, ws: object, **params: object) -> bool:
        """Capture route parameters while rejecting the test handshake."""
        self.params = params
        PathResource.instances.append(self)
        return False


class SessionResource(WebSocketResource):
    """Record frames and one disconnect notification for session tests."""

    instances: typ.ClassVar[list[SessionResource]] = []

    def __init__(self) -> None:
        self.frames: list[str | bytes] = []
        self.disconnect_codes: list[int] = []
        SessionResource.instances.append(self)

    async def on_connect(self, req: object, ws: object, **params: object) -> bool:
        """Accept the connection for the session tests."""
        return True

    async def on_unhandled(self, ws: object, message: str | bytes) -> None:
        """Record raw frames without requiring an application handler."""
        self.frames.append(message)

    async def on_disconnect(self, ws: object, close_code: int) -> None:
        """Record the one close code delivered by the router."""
        self.disconnect_codes.append(close_code)


class AttachedPathResource(WebSocketResource):
    """Capture a parameterized route reached through Falcon's ASGI app."""

    instances: typ.ClassVar[list[AttachedPathResource]] = []

    def __init__(self) -> None:
        self.params: dict[str, object] = {}
        self.received: list[str | bytes] = []
        self.received_event = asyncio.Event()
        AttachedPathResource.instances.append(self)

    async def on_connect(self, req: object, ws: object, **params: object) -> bool:
        """Save the route match and accept the simulated ASGI handshake."""
        self.params = params
        return True

    async def on_unhandled(self, ws: object, message: str | bytes) -> None:
        """Record the raw frame received over the mounted Falcon route."""
        self.received.append(message)
        self.received_event.set()


class NestedAttachedPathResource(WebSocketResource):
    """Mount a final responder below a workspace path parameter."""

    def __init__(self) -> None:
        self.add_subroute("projects/{project_id}/tasks", AttachedPathResource)


class _QueuedWebSocket(RecordingWS):
    """Recording socket that returns queued frames, then a disconnect."""

    def __init__(
        self, frames: cabc.Iterable[object], *, close_code: int = 1000
    ) -> None:
        super().__init__()
        self.frames = list(frames)
        self.disconnect_code = close_code
        self.receive_started = asyncio.Event()

    @typ.override
    async def receive_media(self) -> object:
        self.receive_started.set()
        if self.frames:
            return self.frames.pop(0)
        raise _PeerDisconnectedError(code=self.disconnect_code)


def test_router_attach_registers_asgi_routes_and_wraps_media_once() -> None:
    """Attach registers mount routes and installs media wrappers idempotently."""

    class RouteRecordingApp(falcon.asgi.App):
        def __init__(self) -> None:
            super().__init__()
            self.registered_routes: list[str] = []

        @typ.override
        def add_route(
            self, uri_template: str, resource: object, **kwargs: object
        ) -> None:
            self.registered_routes.append(uri_template)
            super().add_route(uri_template, resource, **kwargs)

    router = WebSocketRouter()
    router.add_route("/rooms/{room}", PathResource, name="room")
    app = RouteRecordingApp()
    router.attach(app, "/ws/")
    text_handler = app.ws_options.media_handlers[falcon.WebSocketPayloadType.TEXT]
    binary_handler = app.ws_options.media_handlers[falcon.WebSocketPayloadType.BINARY]

    second_router = WebSocketRouter()
    second_router.add_route("/events", PathResource)
    second_router.attach(app, "/events")

    assert app.registered_routes == [
        "/ws",
        "/ws/{_falcon_pachinko_path:path}",
        "/events",
        "/events/{_falcon_pachinko_path:path}",
    ], "attach should register an exact mount and a descendant catch-all"
    assert (
        app.ws_options.media_handlers[falcon.WebSocketPayloadType.TEXT] is text_handler
    ), "attaching a second router must preserve the wrapped text handler"
    assert (
        app.ws_options.media_handlers[falcon.WebSocketPayloadType.BINARY]
        is binary_handler
    ), "attaching a second router must preserve the wrapped binary handler"


def test_router_attach_requires_falcon_asgi_app() -> None:
    """Attach rejects objects that cannot serve Falcon ASGI routes."""
    with pytest.raises(TypeError, match=r"falcon\.asgi\.App"):
        WebSocketRouter().attach(object(), "/ws")


def test_router_attach_does_not_warn_for_falcon_default_media_handlers() -> None:
    """Fresh bound default handlers compare by configuration, not identity."""
    app = falcon.asgi.App()

    with warnings.catch_warnings():
        warnings.simplefilter("error")
        WebSocketRouter().attach(app, "/ws")


def test_root_router_attach_registers_exact_and_descendant_routes() -> None:
    """Register the root route and a path converter for descendants."""

    class RouteRecordingApp(falcon.asgi.App):
        def __init__(self) -> None:
            super().__init__()
            self.registered_routes: list[str] = []

        @typ.override
        def add_route(
            self, uri_template: str, resource: object, **kwargs: object
        ) -> None:
            self.registered_routes.append(uri_template)
            super().add_route(uri_template, resource, **kwargs)

    app = RouteRecordingApp()
    router = WebSocketRouter()
    router.add_route("/rooms/{room}", PathResource)
    router.attach(app, "/")
    assert app.registered_routes == ["/", "/{_falcon_pachinko_path:path}"], (
        "a root-mounted router must register its exact and descendant routes"
    )


def test_router_attach_requires_a_mount_and_rejects_repeated_attachment() -> None:
    """A router must be mounted and can be attached to Falcon only once."""
    router = WebSocketRouter()
    app = falcon.asgi.App()

    with pytest.raises(RuntimeError, match="mounted"):
        router.attach(app)

    router.mount("/ws")
    router.attach(app)

    with pytest.raises(RuntimeError, match="already attached"):
        router.attach(app)


@pytest.mark.asyncio
async def test_router_attach_dispatches_parameterized_route_through_falcon() -> None:
    """Attach routes real Falcon ASGI scopes to the router and resource."""
    AttachedPathResource.instances.clear()
    app = falcon.asgi.App()
    router = WebSocketRouter(receive_loop=True)
    router.add_route("/rooms/{room}", AttachedPathResource)
    router.mount("/ws")
    router.attach(app)
    conductor = falcon.testing.ASGIConductor(app)

    async with conductor.simulate_ws("/ws/rooms/atlas") as websocket:
        resource = AttachedPathResource.instances[-1]
        await websocket.send_text("ping")
        await asyncio.wait_for(resource.received_event.wait(), timeout=1.0)

    assert resource.params == {"room": "atlas"}, (
        "Falcon path-converter params must be rematched by the router"
    )
    assert resource.received == ["ping"], (
        "the attached app must route raw frames to the parameterized resource"
    )


@pytest.mark.asyncio
async def test_attached_router_resolves_nested_resources_through_falcon() -> None:
    """Falcon's descendant route preserves nested path parameters and frames."""
    AttachedPathResource.instances.clear()
    app = falcon.asgi.App()
    router = WebSocketRouter(receive_loop=True)
    router.add_route("/workspaces/{workspace_id}", NestedAttachedPathResource)
    router.mount("/ws")
    router.attach(app)
    conductor = falcon.testing.ASGIConductor(app)

    async with conductor.simulate_ws(
        "/ws/workspaces/atlas/projects/triage/tasks"
    ) as websocket:
        resource = AttachedPathResource.instances[-1]
        await websocket.send_text("task.add")
        await asyncio.wait_for(resource.received_event.wait(), timeout=1.0)

    assert resource.params == {"workspace_id": "atlas", "project_id": "triage"}, (
        "Falcon routing must preserve parameters across the nested resource chain"
    )
    assert resource.received == ["task.add"], (
        "the final nested resource must receive frames from Falcon's responder"
    )


@pytest.mark.asyncio
async def test_attached_router_avoids_mount_parameter_capture_collision() -> None:
    """The generated descendant field stays unique from mount parameters."""
    AttachedPathResource.instances.clear()
    app = falcon.asgi.App()
    router = WebSocketRouter(receive_loop=True)
    router.add_route("/rooms", AttachedPathResource)
    router.mount("/{_falcon_pachinko_path}")
    router.attach(app)
    conductor = falcon.testing.ASGIConductor(app)

    async with conductor.simulate_ws("/tenant/rooms") as websocket:
        resource = AttachedPathResource.instances[-1]
        await websocket.send_text("ping")
        await asyncio.wait_for(resource.received_event.wait(), timeout=1.0)

    assert resource.received == ["ping"], (
        "a private Falcon capture must not shadow a mounted route parameter"
    )


@pytest.mark.asyncio
async def test_router_attach_preserves_binary_frames_and_serializes_responses() -> None:
    """Preserve inbound bytes and use the wrapped binary serializer on send."""

    class BinaryMediaHandler:
        def __init__(self) -> None:
            self.serialized: list[object] = []

        def deserialize(self, payload: object) -> object:
            return b"decoded by the original handler"

        def serialize(self, media: object) -> bytes:
            self.serialized.append(media)
            assert isinstance(media, bytes), "binary media must remain bytes"
            return b"serialized:" + media

    class BinaryEchoResource(WebSocketResource):
        instances: typ.ClassVar[list[BinaryEchoResource]] = []

        def __init__(self) -> None:
            self.received: str | bytes | None = None
            self.received_event = asyncio.Event()
            type(self).instances.append(self)

        async def on_connect(self, req: object, ws: object, **params: object) -> bool:
            """Accept the connection for the binary-frame regression."""
            return True

        async def on_unhandled(self, ws: object, message: str | bytes) -> None:
            """Record raw input and send it through Falcon's binary serializer."""
            self.received = message
            self.received_event.set()
            socket = typ.cast("falcon.asgi.WebSocket", ws)
            await socket.send_media(message, falcon.WebSocketPayloadType.BINARY)

    payload = b"\x00\x01raw binary frame"
    BinaryEchoResource.instances.clear()
    binary_handler = BinaryMediaHandler()
    app = falcon.asgi.App()
    media_handlers = typ.cast(
        "cabc.MutableMapping[falcon.WebSocketPayloadType, object]",
        app.ws_options.media_handlers,
    )
    media_handlers[falcon.WebSocketPayloadType.BINARY] = binary_handler
    router = WebSocketRouter(receive_loop=True)
    router.add_route("/binary", BinaryEchoResource)
    with pytest.warns(UserWarning, match="BINARY"):
        router.attach(app, "/ws")
    conductor = falcon.testing.ASGIConductor(app)

    async with conductor.simulate_ws("/ws/binary") as websocket:
        await websocket.send_data(payload)
        assert BinaryEchoResource.instances, "the binary route must create a resource"
        resource = BinaryEchoResource.instances[-1]
        await asyncio.wait_for(resource.received_event.wait(), timeout=1.0)
        response = await websocket.receive_data()

    assert resource.received == payload, "binary input must reach the handler unchanged"
    assert binary_handler.serialized == [payload], (
        "the installed wrapper must delegate binary serialization"
    )
    assert response == b"serialized:" + payload, (
        "the peer must receive the configured binary serialization"
    )


@pytest.mark.asyncio
async def test_attach_documents_raw_media_contract_for_other_falcon_routes() -> None:
    """Falcon responders on the same app observe the installed raw handlers."""

    class PlainWebSocketResource:
        def __init__(self) -> None:
            self.received: str | bytes | None = None
            self.received_event = asyncio.Event()

        async def on_websocket(self, req: object, ws: WebSocketLike) -> None:
            await ws.accept()
            payload = await ws.receive_media()
            assert isinstance(payload, str | bytes), "raw media must stay a frame"
            self.received = payload
            self.received_event.set()
            await ws.close()

    app = falcon.asgi.App()
    plain_resource = PlainWebSocketResource()
    app.add_route("/plain", plain_resource)
    router = WebSocketRouter()
    router.add_route("/rooms/{room}", AttachedPathResource)
    router.attach(app, "/ws")
    conductor = falcon.testing.ASGIConductor(app)

    async with conductor.simulate_ws("/plain") as websocket:
        await websocket.send_text('{"answer": 42}')
        await asyncio.wait_for(plain_resource.received_event.wait(), timeout=1.0)

    assert plain_resource.received == '{"answer": 42}', (
        "all Falcon WebSocket responders on an attached app receive raw frames"
    )


def test_attach_warns_when_websocket_media_handlers_are_customized() -> None:
    """Warn when attach replaces custom app-wide WebSocket deserializers."""

    class CustomTextHandler:
        def deserialize(self, payload: object) -> object:
            return {"decoded": payload}

        def serialize(self, media: object) -> object:
            return media

    app = falcon.asgi.App()
    media_handlers = typ.cast(
        "cabc.MutableMapping[falcon.WebSocketPayloadType, object]",
        app.ws_options.media_handlers,
    )
    media_handlers[falcon.WebSocketPayloadType.TEXT] = CustomTextHandler()

    with pytest.warns(UserWarning, match="app-wide WebSocket") as caught:
        WebSocketRouter().attach(app, "/ws")

    assert caught[0].filename == __file__, (
        "the warning should identify the application attach() call site"
    )


def test_attach_warns_for_customized_falcon_json_handler() -> None:
    """Warn when a Falcon handler stores custom settings in slots."""
    app = falcon.asgi.App()
    media_handlers = typ.cast(
        "cabc.MutableMapping[falcon.WebSocketPayloadType, object]",
        app.ws_options.media_handlers,
    )
    media_handlers[falcon.WebSocketPayloadType.TEXT] = falcon.media.JSONHandlerWS(
        loads=lambda payload: {"decoded": payload}
    )

    with pytest.warns(UserWarning, match="app-wide WebSocket"):
        WebSocketRouter().attach(app, "/ws")


@pytest.mark.asyncio
async def test_on_websocket_rejects_unregistered_descendant_template() -> None:
    """Only the catch-all route registered by attach may dispatch descendants."""
    router = WebSocketRouter()
    router.add_route("/admin", PathResource)
    router.mount("/ws")

    with pytest.raises(falcon.HTTPNotFound):
        await router.on_websocket(make_req("/ws/admin", "/ws/admin"), DummyWS())


@pytest.mark.asyncio
async def test_on_websocket_dispatches_multiple_frames_and_cleans_once() -> None:
    """A session dispatches every raw frame and keeps the disconnect code."""
    SessionResource.instances.clear()
    events: list[str] = []

    class OrderedSessionResource(SessionResource):
        async def on_unhandled(self, ws: object, message: str | bytes) -> None:
            events.append("dispatch")
            await super().on_unhandled(ws, message)

        async def on_disconnect(self, ws: object, close_code: int) -> None:
            events.append("resource.disconnect")
            await super().on_disconnect(ws, close_code)

    router = WebSocketRouter(receive_loop=True)
    router.global_hooks.add(
        "before_disconnect", lambda _context: events.append("hook.disconnect")
    )
    router.add_route("/session", OrderedSessionResource)
    router.mount("/")
    frames = ['{"type":"first"}', b'{"type":"second"}']
    ws = _QueuedWebSocket(frames, close_code=1001)

    await router.on_websocket(make_req("/session"), ws)

    resource = SessionResource.instances[-1]
    assert resource.frames == frames, "each raw frame must reach dispatch"
    assert resource.disconnect_codes == [1001], "disconnect cleanup runs once"
    assert events == [
        "dispatch",
        "dispatch",
        "hook.disconnect",
        "resource.disconnect",
    ], "message dispatch must precede hooks and resource disconnect cleanup"


@given(
    frames=st.lists(st.one_of(st.text(), st.binary()), max_size=10),
    close_code=st.sampled_from((
        1000,
        1001,
        1002,
        1003,
        1007,
        1008,
        1009,
        1010,
        1011,
        1012,
        1013,
        1014,
    )),
)
@example(frames=[], close_code=1000)
@example(frames=["first", b"second"], close_code=1001)
def test_generated_session_frames_preserve_order_and_disconnect_code(
    frames: list[str | bytes], close_code: int
) -> None:
    """Preserve arbitrary raw frame order through a clean peer disconnect."""

    async def exercise_session() -> tuple[list[str | bytes], list[int]]:
        """Run one isolated session and return its observed lifecycle data."""
        SessionResource.instances.clear()
        router = WebSocketRouter(receive_loop=True)
        router.add_route("/session", SessionResource)
        router.mount("/")
        ws = _QueuedWebSocket(frames, close_code=close_code)

        await router.on_websocket(make_req("/session"), ws)

        resource = SessionResource.instances[-1]
        return resource.frames, resource.disconnect_codes

    received_frames, disconnect_codes = asyncio.run(exercise_session())

    assert received_frames == frames, "all generated frames must keep their order"
    assert disconnect_codes == [close_code], (
        "a peer disconnect must notify cleanup exactly once with its close code"
    )


@pytest.mark.asyncio
async def test_cancelled_session_runs_cleanup_then_reraises() -> None:
    """Cancellation reports 1006 to cleanup and remains visible to the caller."""
    SessionResource.instances.clear()

    class BlockingWebSocket(RecordingWS):
        @typ.override
        async def receive_media(self) -> object:
            self.receive_started.set()
            await asyncio.Event().wait()

        def __init__(self) -> None:
            super().__init__()
            self.receive_started = asyncio.Event()

    router = WebSocketRouter(receive_loop=True)
    router.add_route("/session", SessionResource)
    router.mount("/")
    ws = BlockingWebSocket()
    task = asyncio.create_task(router.on_websocket(make_req("/session"), ws))
    await ws.receive_started.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert not ws.closed, "cancellation must not send reserved close code 1006"
    assert SessionResource.instances[-1].disconnect_codes == [1006], (
        "server cancellation must reach disconnect cleanup as abnormal closure"
    )


@pytest.mark.asyncio
async def test_dispatch_error_closes_with_1011_and_runs_cleanup() -> None:
    """A handler error propagates after 1011 close and cleanup."""

    class ExplodingResource(SessionResource):
        async def on_explode(self, ws: object, payload: object) -> None:
            raise RuntimeError

    router = WebSocketRouter(receive_loop=True)
    router.add_route("/session", ExplodingResource)
    router.mount("/")
    ws = _QueuedWebSocket(['{"type":"explode"}'])

    with pytest.raises(RuntimeError):
        await router.on_websocket(make_req("/session"), ws)

    assert ws.closed == [1011], "dispatch errors use the internal-error close"
    assert ExplodingResource.instances[-1].disconnect_codes == [1011], (
        "dispatch failure must be reported to disconnect cleanup"
    )


@pytest.mark.asyncio
async def test_disconnect_raised_during_dispatch_is_a_normal_disconnect() -> None:
    """A send failure caused by peer disconnect keeps the peer close code."""

    class DisconnectingResource(SessionResource):
        async def on_unhandled(self, ws: object, message: str | bytes) -> None:
            raise falcon.WebSocketDisconnected(code=1001)

    router = WebSocketRouter(receive_loop=True)
    router.add_route("/session", DisconnectingResource)
    router.mount("/")
    ws = _QueuedWebSocket(['{"type":"push"}'])

    await router.on_websocket(make_req("/session"), ws)

    assert not ws.closed, "a peer disconnect must not send an internal-error close"
    assert DisconnectingResource.instances[-1].disconnect_codes == [1001], (
        "disconnect cleanup must receive the peer's code"
    )


@pytest.mark.asyncio
async def test_path_template_strips_a_terminal_greedy_capture() -> None:
    """The selected path template is normalized before mount validation."""
    PathResource.instances.clear()
    router = WebSocketRouter()
    router.add_route("/rooms/{room}", PathResource)
    router.mount("/ws")
    req = type(
        "Req",
        (),
        {
            "path": "/ws/rooms/42",
            "path_template": "/ws/{_private_path:path}",
            "uri_template": "/wrong",
        },
    )()

    await router.on_websocket(req, DummyWS())

    assert PathResource.instances[-1].params == {"room": "42"}, (
        "path_template must take precedence and drop its terminal path capture"
    )


@pytest.mark.asyncio
async def test_uri_template_fallback_strips_a_terminal_greedy_capture() -> None:
    """The router uses uri_template when path_template is absent."""
    PathResource.instances.clear()
    router = WebSocketRouter()
    router.add_route("/rooms/{room}", PathResource)
    router.mount("/ws")
    req = type(
        "Req",
        (),
        {
            "path": "/ws/rooms/42",
            "path_template": "",
            "uri_template": "/ws/{_private_path:path}",
        },
    )()

    await router.on_websocket(req, DummyWS())

    assert PathResource.instances[-1].params == {"room": "42"}, (
        "uri_template fallback must strip its terminal greedy path field"
    )


@pytest.mark.asyncio
async def test_descendant_template_and_falcon_keyword_params_are_accepted() -> None:
    """Falcon catch-all params are ignored while the router re-matches path."""
    PathResource.instances.clear()
    router = WebSocketRouter()
    router.add_route("/rooms/{room}", PathResource)
    router.mount("/ws")
    req = make_req("/ws/rooms/42", "/ws/{pachinko_path:path}")

    await router.on_websocket(req, DummyWS(), pachinko_path="rooms/42")

    assert PathResource.instances[-1].params == {"room": "42"}, (
        "Falcon catch-all parameters must not replace router path matching"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("uri_template", "path_template"),
    [
        ("/ws/{rest:path}", "/ws"),
        (None, None),
    ],
    ids=("foreign_path_converter", "missing_path_template"),
)
async def test_unrecognized_mount_templates_are_rejected(
    uri_template: str | None,
    path_template: str | None,
) -> None:
    """Reject foreign catch-alls and absent Falcon templates as not found."""
    router = WebSocketRouter()
    router.add_route("/rooms/{room}", PathResource)
    router.mount("/ws")
    req = type(
        "Req",
        (),
        {
            "path": "/ws/rooms/42",
            "uri_template": uri_template,
            "path_template": path_template,
        },
    )()

    with pytest.raises(falcon.HTTPNotFound):
        await router.on_websocket(req, DummyWS())


@pytest.mark.asyncio
async def test_router_does_not_accept_an_already_accepted_socket() -> None:
    """A resource-owned accept is not repeated by the router."""

    class AcceptingResource(WebSocketResource):
        async def on_connect(
            self, req: object, ws: WebSocketLike, **params: object
        ) -> bool:
            await ws.accept()
            return True

    router = WebSocketRouter()
    router.add_route("/ok", AcceptingResource)
    router.mount("/")
    ws = _QueuedWebSocket([])

    await router.on_websocket(make_req("/ok"), ws)

    assert ws.accepted == [None], "the router must not accept twice"


@pytest.mark.asyncio
async def test_session_adapts_decoded_receive_media_payloads() -> None:
    """Decoded Falcon media is encoded before raw-frame dispatch."""
    SessionResource.instances.clear()
    router = WebSocketRouter(receive_loop=True)
    router.add_route("/session", SessionResource)
    router.mount("/")
    ws = _QueuedWebSocket([{"already": "decoded"}])

    await router.on_websocket(make_req("/session"), ws)

    assert SessionResource.instances[-1].frames == [b'{"already":"decoded"}'], (
        "decoded Falcon media must become the raw JSON frame"
    )
    assert SessionResource.instances[-1].disconnect_codes == [1000], (
        "the finite test peer must disconnect cleanly after its one frame"
    )
