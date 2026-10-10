"""Live Falcon ASGI WebSocket server fixture for router-session scenarios."""

from __future__ import annotations

import asyncio
import dataclasses as dc
import typing as typ

import falcon.asgi
import pytest
import uvicorn

from falcon_pachinko import (
    WebSocketResource,
    WebSocketRouter,
    configure_raw_frame_media,
)
from falcon_pachinko.hooks import HookContext, HookEvent
from falcon_pachinko.testing import WebSocketTestClient

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
    """Expose the real server, test client, and recorder to a scenario."""

    event_loop: asyncio.AbstractEventLoop
    server: _SignalFreeServer
    server_task: asyncio.Task[None]
    base_url: str
    client: WebSocketTestClient
    recorder: SessionRecorder


class _SignalFreeServer(uvicorn.Server):
    """Uvicorn server that leaves signal ownership to the pytest process."""

    def install_signal_handlers(self) -> None:
        """Do not replace the test runner's process signal handlers."""


def _create_app(
    recorder: SessionRecorder,
    resource_factory: cabc.Callable[[SessionRecorder], WebSocketResource],
) -> falcon.asgi.App:
    """Build Falcon with raw frame media and the recording router hook."""
    app = falcon.asgi.App()
    configure_raw_frame_media(app)
    router = WebSocketRouter()
    router.add_route("/", lambda: resource_factory(recorder))

    def record_after_receive(context: HookContext) -> None:
        recorder.after_receive_errors.append(context.error)

    router.global_hooks.add(HookEvent.AFTER_RECEIVE, record_after_receive)
    router.mount("/ws")
    app.add_route("/ws", router)
    return app


def _launch_server(
    event_loop: asyncio.AbstractEventLoop, app: falcon.asgi.App
) -> tuple[_SignalFreeServer, asyncio.Task[None], str]:
    """Start Uvicorn and return its task and bound WebSocket URL."""
    server = _SignalFreeServer(
        uvicorn.Config(
            app,
            host="127.0.0.1",
            port=0,
            lifespan="off",
            log_level="critical",
        )
    )
    server_task = event_loop.create_task(server.serve())

    async def wait_until_started() -> None:
        deadline = event_loop.time() + 10
        while not server.started:
            if server_task.done():
                await server_task
                msg = "Uvicorn stopped before its live server started"
                raise RuntimeError(msg)
            if event_loop.time() >= deadline:
                msg = "Uvicorn live server did not start within 10 seconds"
                raise TimeoutError(msg)
            await asyncio.sleep(0.01)

    event_loop.run_until_complete(asyncio.wait_for(wait_until_started(), timeout=10))
    bound_servers = server.servers
    if not bound_servers or not bound_servers[0].sockets:
        msg = "Uvicorn started without exposing a bound server socket"
        raise RuntimeError(msg)
    host, port, *_ = bound_servers[0].sockets[0].getsockname()
    return server, server_task, f"ws://{host}:{port}"


def _start_live_context(
    event_loop: asyncio.AbstractEventLoop,
    resource_factory: cabc.Callable[[SessionRecorder], WebSocketResource],
) -> LiveServerContext:
    """Build the test recorder, Falcon app, and running Uvicorn context."""
    recorder = SessionRecorder()
    app = _create_app(recorder, resource_factory)
    server, server_task, base_url = _launch_server(event_loop, app)
    client = WebSocketTestClient(base_url=base_url, allow_insecure=True)
    return LiveServerContext(
        event_loop=event_loop,
        server=server,
        server_task=server_task,
        base_url=base_url,
        client=client,
        recorder=recorder,
    )


@pytest.fixture
def event_loop(
    event_loop_policy: asyncio.AbstractEventLoopPolicy,
) -> cabc.Iterator[asyncio.AbstractEventLoop]:
    """Provide a dedicated event loop for the live WebSocket server."""
    loop = event_loop_policy.new_event_loop()
    try:
        asyncio.set_event_loop(loop)
        yield loop
    finally:
        asyncio.set_event_loop(None)
        loop.close()


@pytest.fixture
def live_server(
    event_loop: asyncio.AbstractEventLoop,
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
        context = _start_live_context(event_loop, resource_factory)
        contexts.append(context)
        return context

    try:
        yield start
    finally:
        for context in contexts:
            context.server.should_exit = True
            event_loop.run_until_complete(
                asyncio.wait_for(context.server_task, timeout=10)
            )
