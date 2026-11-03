"""Live Falcon ASGI WebSocket server fixture for router-session scenarios."""

from __future__ import annotations

import asyncio
import dataclasses as dc
import typing as typ

import falcon.asgi
import pytest

from falcon_pachinko import (
    WebSocketResource,
    WebSocketRouter,
    configure_raw_frame_media,
)
from falcon_pachinko.hooks import HookContext, HookEvent
from falcon_pachinko.testing import WebSocketTestClient

from ._live_server import LiveServerTransport, _assert_no_loop_errors
from ._live_server import SignalFreeServer as _SignalFreeServer

if typ.TYPE_CHECKING:
    import collections.abc as cabc


@dc.dataclass(slots=True)
class SessionRecorder:
    """Collect handler, fallback, hook, failure, and disconnect observations."""

    handler_calls: list[str] = dc.field(default_factory=list)
    unhandled_messages: list[str | bytes] = dc.field(default_factory=list)
    disconnect_codes: list[int] = dc.field(default_factory=list)
    after_receive_errors: list[BaseException | None] = dc.field(default_factory=list)
    handler_failure: BaseException | None = None
    disconnected: asyncio.Event = dc.field(default_factory=asyncio.Event)


@dc.dataclass(slots=True)
class LiveServerContext:
    """Expose the shared server transport and recorder to a router scenario."""

    event_loop: asyncio.AbstractEventLoop
    transport: LiveServerTransport
    client: WebSocketTestClient
    recorder: SessionRecorder


def _create_app(
    recorder: SessionRecorder,
    resource_factory: cabc.Callable[[SessionRecorder], WebSocketResource],
) -> falcon.asgi.App:
    """Build Falcon with raw frame media and the recording router hook."""
    app = falcon.asgi.App()
    configure_raw_frame_media(app)
    router = WebSocketRouter(receive_loop=True)
    router.add_route("/", lambda: resource_factory(recorder))

    def record_after_receive(context: HookContext) -> None:
        recorder.after_receive_errors.append(context.error)

    router.global_hooks.add(HookEvent.AFTER_RECEIVE, record_after_receive)
    router.mount("/ws")
    app.add_route("/ws", router)
    return app


def _start_live_context(
    event_loop: asyncio.AbstractEventLoop,
    resource_factory: cabc.Callable[[SessionRecorder], WebSocketResource],
) -> LiveServerContext:
    """Build the recorder and app, then launch the shared Uvicorn transport."""
    recorder = SessionRecorder()
    app = _create_app(recorder, resource_factory)
    transport = event_loop.run_until_complete(
        LiveServerTransport.launch(event_loop, app, server_factory=_SignalFreeServer)
    )
    client = WebSocketTestClient(base_url=transport.base_url, allow_insecure=True)
    return LiveServerContext(event_loop, transport, client, recorder)


@pytest.fixture
def live_server(
    live_event_loop: asyncio.AbstractEventLoop,
) -> cabc.Iterator[
    cabc.Callable[
        [cabc.Callable[[SessionRecorder], WebSocketResource]], LiveServerContext
    ]
]:
    """Return a server builder that injects one shared recorder per scenario."""
    contexts: list[LiveServerContext] = []

    def start(
        resource_factory: cabc.Callable[[SessionRecorder], WebSocketResource],
    ) -> LiveServerContext:
        context = _start_live_context(live_event_loop, resource_factory)
        contexts.append(context)
        return context

    try:
        yield start
    finally:
        for context in contexts:
            live_event_loop.run_until_complete(context.transport.stop())
            _assert_no_loop_errors(context.transport.loop_errors)
