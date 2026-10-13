"""Tests for the live ASGI server context manager."""

from __future__ import annotations

import asyncio
import sys
import typing as typ

import falcon.asgi
import pytest

from falcon_pachinko.testing import (
    MissingDependencyError,
    WebSocketTestClient,
    serve_asgi_app,
)
from falcon_pachinko.testing.live_server import _wait_for_startup

if typ.TYPE_CHECKING:
    import collections.abc as cabc


class _EchoResource:
    """Exchange one text frame through Falcon's real WebSocket responder."""

    async def on_websocket(
        self, req: falcon.asgi.Request, ws: falcon.asgi.WebSocket
    ) -> None:
        await ws.accept()
        message = await ws.receive_text()
        await ws.send_text(f"echo:{message}")


class _LifecycleMiddleware:
    """Record Falcon lifespan startup and shutdown events."""

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.stopped = asyncio.Event()

    async def process_startup(
        self, scope: cabc.Mapping[str, typ.Any], event: cabc.Mapping[str, typ.Any]
    ) -> None:
        self.started.set()

    async def process_shutdown(
        self, scope: cabc.Mapping[str, typ.Any], event: cabc.Mapping[str, typ.Any]
    ) -> None:
        self.stopped.set()


class _FailedStartupMiddleware:
    """Fail the real Falcon lifespan startup for error propagation coverage."""

    async def process_startup(
        self, scope: cabc.Mapping[str, typ.Any], event: cabc.Mapping[str, typ.Any]
    ) -> None:
        msg = "intentional lifespan startup failure"
        raise RuntimeError(msg)


@pytest.mark.asyncio
async def test_live_server_serves_websockets_and_runs_falcon_lifespan() -> None:
    """The harness binds an ephemeral port and finishes app shutdown."""
    app = falcon.asgi.App()
    lifecycle = _LifecycleMiddleware()
    app.add_middleware(lifecycle)
    app.add_route("/echo", _EchoResource())

    async with serve_asgi_app(app) as base_url:
        assert base_url.startswith("ws://127.0.0.1:"), (
            "the harness must expose its ephemeral loopback listener"
        )
        assert lifecycle.started.is_set(), "Falcon startup must run before yielding"
        client = WebSocketTestClient(base_url, allow_insecure=True)
        async with client.connect("/echo") as session:
            await session.send_text("hello")
            reply = await session.receive_text()
        assert reply == "echo:hello", "the live Falcon responder must echo text"

    assert lifecycle.stopped.is_set(), "Falcon shutdown must run before exit"


@pytest.mark.asyncio
async def test_live_server_reports_missing_uvicorn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The optional server dependency fails only when the harness is used."""
    monkeypatch.setitem(sys.modules, "uvicorn", None)

    with pytest.raises(MissingDependencyError, match="uvicorn"):
        async with serve_asgi_app(falcon.asgi.App()):
            pytest.fail("a missing Uvicorn installation must stop startup")


@pytest.mark.asyncio
async def test_live_server_surfaces_falcon_lifespan_startup_failure() -> None:
    """A failing Falcon startup is reported without stopping the test loop."""
    app = falcon.asgi.App()
    app.add_middleware(_FailedStartupMiddleware())

    with pytest.raises(RuntimeError, match="ASGI lifespan startup failed"):
        async with serve_asgi_app(app, startup_timeout=1.0):
            pytest.fail("a failed app startup must stop the context from yielding")


@pytest.mark.asyncio
async def test_startup_wait_surfaces_an_early_server_failure() -> None:
    """A failed lifespan startup is surfaced instead of timing out."""

    class ServerState:
        started = False

    async def fail_startup() -> None:
        await asyncio.sleep(0)
        msg = "lifespan startup failed"
        raise RuntimeError(msg)

    task = asyncio.create_task(fail_startup())
    with pytest.raises(RuntimeError, match="lifespan startup failed"):
        await _wait_for_startup(ServerState(), task, timeout_seconds=1.0)


@pytest.mark.asyncio
async def test_startup_wait_enforces_its_timeout() -> None:
    """A server that remains unstarted is stopped at its deadline."""

    class ServerState:
        started = False

    async def remain_unstarted() -> None:
        await asyncio.Event().wait()

    task = asyncio.create_task(remain_unstarted())
    with pytest.raises(TimeoutError, match="did not start"):
        await _wait_for_startup(ServerState(), task, timeout_seconds=0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
