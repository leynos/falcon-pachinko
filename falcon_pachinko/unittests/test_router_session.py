"""Focused tests for Falcon WebSocket attachment and session handling."""

from __future__ import annotations

import asyncio
import typing as typ

import falcon
import falcon.asgi
import falcon.testing
import pytest

from falcon_pachinko import WebSocketResource, WebSocketRouter
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
        raise falcon.WebSocketDisconnected(code=self.disconnect_code)


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
        "/ws/{pachinko_path:path}",
        "/events",
        "/events/{pachinko_path:path}",
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


def test_root_router_attach_registers_path_converter() -> None:
    """A root-mounted router uses a path converter for descendant paths."""

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
    assert app.registered_routes == ["/{pachinko_path:path}"], (
        "a root-mounted router must register its descendant path converter"
    )


@pytest.mark.asyncio
async def test_router_attach_dispatches_parameterized_route_through_falcon() -> None:
    """Attach routes real Falcon ASGI scopes to the router and resource."""
    AttachedPathResource.instances.clear()
    app = falcon.asgi.App()
    router = WebSocketRouter()
    router.add_route("/rooms/{room}", AttachedPathResource)
    router.attach(app, "/ws")
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
    router = WebSocketRouter()
    router.add_route("/session", SessionResource)
    router.mount("/")
    frames = ['{"type":"first"}', b'{"type":"second"}']
    ws = _QueuedWebSocket(frames, close_code=1001)

    await router.on_websocket(make_req("/session"), ws)

    resource = SessionResource.instances[-1]
    assert resource.frames == frames, "each raw frame must reach dispatch"
    assert resource.disconnect_codes == [1001], "disconnect cleanup runs once"


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

    router = WebSocketRouter()
    router.add_route("/session", SessionResource)
    router.mount("/")
    ws = BlockingWebSocket()
    task = asyncio.create_task(router.on_websocket(make_req("/session"), ws))
    await ws.receive_started.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert SessionResource.instances[-1].disconnect_codes == [1006], (
        "cancellation must reach disconnect cleanup as abnormal closure"
    )


@pytest.mark.asyncio
async def test_dispatch_error_closes_with_1011_and_runs_cleanup() -> None:
    """A handler error propagates after 1011 close and cleanup."""

    class ExplodingResource(SessionResource):
        async def on_explode(self, ws: object, payload: object) -> None:
            raise RuntimeError

    router = WebSocketRouter()
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
async def test_uri_template_takes_precedence_over_path_template() -> None:
    """Falcon's selected uri_template is preferred when routing a request."""
    PathResource.instances.clear()
    router = WebSocketRouter()
    router.add_route("/rooms/{room}", PathResource)
    router.mount("/ws")
    req = type(
        "Req",
        (),
        {
            "path": "/ws/rooms/42",
            "uri_template": "/ws/{pachinko_path:path}",
            "path_template": "/wrong",
        },
    )()

    await router.on_websocket(req, DummyWS())

    assert PathResource.instances[-1].params == {"room": "42"}, (
        "the router must prefer Falcon's selected URI template"
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
async def test_session_rejects_non_raw_receive_media_payloads() -> None:
    """Decoded objects fail with guidance to attach the router to Falcon."""

    class DecodedWebSocket(RecordingWS):
        @typ.override
        async def receive_media(self) -> object:
            return {"already": "decoded"}

    router = WebSocketRouter()
    router.add_route("/session", SessionResource)
    router.mount("/")

    with pytest.raises(TypeError, match=r"WebSocketRouter\.attach"):
        await router.on_websocket(make_req("/session"), DecodedWebSocket())

    assert SessionResource.instances[-1].disconnect_codes == [1011], (
        "invalid decoded payloads must run failure cleanup"
    )
