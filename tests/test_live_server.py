"""Startup and teardown boundaries of the live ASGI server helper."""

from __future__ import annotations

import asyncio
import typing as typ

import falcon.asgi
import pytest
import uvicorn

from tests._live_server import live_server

if typ.TYPE_CHECKING:
    import socket


class _EarlyServer(uvicorn.Server):
    """Exit before signalling startup."""

    @typ.override
    async def serve(self, sockets: list[socket.socket] | None = None) -> None:
        """Yield once and return without starting."""
        await asyncio.sleep(0)


class _FailedServer(uvicorn.Server):
    """Fail before signalling startup."""

    @typ.override
    async def serve(self, sockets: list[socket.socket] | None = None) -> None:
        """Expose the original startup failure."""
        msg = "startup failed"
        raise RuntimeError(msg)


@pytest.mark.asyncio
@pytest.mark.parametrize("server_type", [_EarlyServer, _FailedServer])
async def test_live_server_reports_early_exit(
    monkeypatch: pytest.MonkeyPatch,
    server_type: type[uvicorn.Server],
) -> None:
    """Startup never waits out the deadline after a server task has ended."""
    monkeypatch.setattr(uvicorn, "Server", server_type)
    with pytest.raises(RuntimeError):
        async with live_server(falcon.asgi.App(), startup_timeout=0.5):
            pytest.fail("an exited server must never be exposed as ready")


@pytest.mark.asyncio
async def test_live_server_bounds_stalled_shutdown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A helper-owned stalled server task is cancelled and awaited on exit."""
    stopped = asyncio.Event()

    class StalledServer(uvicorn.Server):
        @typ.override
        async def serve(self, sockets: list[socket.socket] | None = None) -> None:
            self.started = True
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()

    monkeypatch.setattr(uvicorn, "Server", StalledServer)
    async with asyncio.timeout(1):
        async with live_server(falcon.asgi.App(), shutdown_timeout=0.02) as url:
            assert url.startswith("ws://127.0.0.1:"), (
                "the URL must use the prebound loopback port"
            )
    assert stopped.is_set(), "the helper must await its server task during teardown"
