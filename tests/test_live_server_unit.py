"""Unit tests for live ASGI server lifecycle and error handling."""

from __future__ import annotations

import asyncio
import socket
import typing as typ

import falcon.asgi
import pytest

from falcon_pachinko import WebSocketResource, WebSocketRouter
from falcon_pachinko.testing import (
    LiveServerError,
    LiveServerShutdownError,
    LiveServerStartupError,
    LiveWebSocketServer,
    MissingDependencyError,
)

if typ.TYPE_CHECKING:
    from falcon_pachinko.testing.live import ASGIApplication


class _AcceptingResource(WebSocketResource):
    async def on_connect(self, req: object, ws: object, **params: object) -> bool:
        """Keep the connection open until the client disconnects."""
        return True


class _FailingHTTPResource:
    async def on_get(self, req: object, resp: object) -> None:
        """Raise through Falcon's HTTP responder path."""
        msg = "HTTP responder failed"
        raise RuntimeError(msg)


class _FailingStartupMiddleware:
    async def process_startup(self, scope: object, event: object) -> None:
        """Fail Falcon's ASGI lifespan startup handshake."""
        msg = "Falcon lifespan startup failed"
        raise RuntimeError(msg)


class _FakeAdapter:
    """Provide deterministic readiness and stop behavior without a server."""

    def __init__(
        self,
        *,
        start_error: Exception | None = None,
        hang_on_stop: bool = False,
    ) -> None:
        self._finished = asyncio.Event()
        self.task = asyncio.create_task(self._serve())
        self.start_error = start_error
        self.hang_on_stop = hang_on_stop
        self.started = asyncio.Event()
        self.socket: socket.socket | None = None
        self.app: ASGIApplication | None = None

    async def _serve(self) -> None:
        await self._finished.wait()

    async def start(self, app: ASGIApplication, sock: socket.socket) -> None:
        self.socket = sock
        self.app = app
        if self.start_error is not None:
            raise self.start_error
        self.started.set()

    async def stop(self) -> None:
        if self.hang_on_stop:
            await asyncio.Future()
        self._finished.set()
        await self.task


class _FailingServeAdapter(_FakeAdapter):
    """Exit after readiness to exercise serve-task error reporting."""

    def __init__(self) -> None:
        super().__init__()
        self._fail_on_exit = False

    async def _serve(self) -> None:
        await self._finished.wait()
        if self._fail_on_exit:
            msg = "serve task failed"
            raise RuntimeError(msg)

    def fail(self) -> None:
        self._fail_on_exit = True
        self._finished.set()


def _make_app() -> falcon.asgi.App:
    app = falcon.asgi.App()
    router = WebSocketRouter()
    router.add_route("/socket", _AcceptingResource)
    router.attach(app, "/ws")
    app.add_route("/fail", _FailingHTTPResource())
    return app


@pytest.mark.asyncio
async def test_base_url_uses_ephemeral_loopback_port() -> None:
    """Expose the ephemeral IPv4 listener through a WebSocket URL."""
    adapter = _FakeAdapter()
    async with LiveWebSocketServer(_make_app(), adapter=adapter) as server:
        host, port = server.base_url.removeprefix("ws://").split(":")
        assert host == "127.0.0.1", "the live server must bind to loopback"
        assert int(port) > 0, "the listener must use an allocated port"
        assert adapter.socket is not None, "the adapter must receive the bound socket"
        assert adapter.socket.getsockname()[1] == int(port), (
            "the published URL port must match the prebound listener"
        )


@pytest.mark.asyncio
async def test_concurrent_servers_receive_distinct_ephemeral_ports() -> None:
    """Allocate independent ephemeral listeners for concurrent servers."""

    async def port_for() -> int:
        async with LiveWebSocketServer(_make_app()) as server:
            port = int(server.base_url.rsplit(":", maxsplit=1)[1])
        assert port > 0, "each live listener must use a valid ephemeral port"
        return port

    first, second = await asyncio.gather(port_for(), port_for())

    assert first != second, "concurrent live servers must not share a port"


@pytest.mark.asyncio
async def test_startup_failure_raises_before_entering_context() -> None:
    """Wrap adapter startup errors before exposing a live context."""
    adapter = _FakeAdapter(start_error=RuntimeError("lifespan failed"))

    async def start_server() -> None:
        async with LiveWebSocketServer(_make_app(), adapter=adapter):
            pytest.fail("the context must not yield after startup fails")

    with pytest.raises(LiveServerStartupError) as caught:
        await start_server()

    assert isinstance(caught.value.__cause__, RuntimeError), (
        "startup failure must retain its original cause"
    )
    assert str(caught.value.__cause__) == "lifespan failed", (
        "startup failure must retain the adapter's message"
    )


@pytest.mark.asyncio
async def test_falcon_lifespan_failure_raises_before_entering_context() -> None:
    """Report Falcon lifespan errors before yielding the test server."""
    app = falcon.asgi.App(middleware=_FailingStartupMiddleware())
    router = WebSocketRouter()
    router.add_route("/socket", _AcceptingResource)
    router.attach(app, "/ws")

    async def start_server() -> None:
        async with LiveWebSocketServer(app, startup_timeout=1.0):
            pytest.fail("the context must not yield after lifespan failure")

    with pytest.raises(LiveServerStartupError):
        await start_server()


@pytest.mark.asyncio
async def test_teardown_closes_an_open_websocket_session() -> None:
    """Close sessions still tracked when the live server exits."""
    server = LiveWebSocketServer(_make_app())
    # pylint: disable-next=unnecessary-dunder-call  # The test controls the context lifetime explicitly.
    await server.__aenter__()
    connection_context = server.connect("/ws/socket")
    session = await connection_context.__aenter__()

    await server.__aexit__(None, None, None)
    await connection_context.__aexit__(None, None, None)

    assert session.closed, "server teardown must close tracked client sessions"


@pytest.mark.asyncio
async def test_shutdown_timeout_cancels_serve_task_and_records_failure() -> None:
    """Cancel and report an adapter task that misses its shutdown deadline."""
    adapter = _FakeAdapter(hang_on_stop=True)

    with pytest.raises(LiveServerError) as caught:
        async with LiveWebSocketServer(
            _make_app(), adapter=adapter, shutdown_timeout=0.01
        ):
            pass

    assert any(
        isinstance(error, LiveServerShutdownError) for error in caught.value.exceptions
    ), "timeout fallback must be reported as a shutdown error"
    assert adapter.task.cancelled(), "a timed-out server task must be cancelled"


@pytest.mark.asyncio
async def test_unacknowledged_captured_error_fails_teardown() -> None:
    """Raise captured application failures that were not acknowledged."""
    failure = RuntimeError("captured failure")

    with pytest.raises(LiveServerError) as caught:
        async with LiveWebSocketServer(_make_app(), adapter=_FakeAdapter()) as server:
            server.record_error(failure)

    assert caught.value.exceptions == (failure,), (
        "teardown must report exactly the unacknowledged error"
    )


@pytest.mark.asyncio
async def test_pop_errors_acknowledges_captured_failures() -> None:
    """Acknowledge captured failures by removing them from the snapshot."""
    failure = RuntimeError("handled failure")

    async with LiveWebSocketServer(_make_app(), adapter=_FakeAdapter()) as server:
        server.record_error(failure)
        assert server.errors == (failure,), "errors must expose the captured failure"
        assert server.pop_errors() == (failure,), (
            "pop_errors must return captured errors"
        )
        assert server.errors == (), "pop_errors must acknowledge all captured errors"


@pytest.mark.asyncio
async def test_escaped_asgi_exception_is_recorded_and_reraised() -> None:
    """Record failures escaping the ASGI app while preserving propagation."""

    class EscapingApp(falcon.asgi.App):
        async def __call__(self, scope: object, receive: object, send: object) -> None:
            msg = "escaped ASGI failure"
            raise RuntimeError(msg)

    adapter = _FakeAdapter()

    async with LiveWebSocketServer(EscapingApp(), adapter=adapter) as server:
        assert adapter.app is not None, "the adapter must receive the wrapped ASGI app"
        with pytest.raises(RuntimeError, match="escaped ASGI failure"):
            await adapter.app({}, None, None)
        assert len(server.errors) == 1, "escaped ASGI failures must be recorded once"
        assert isinstance(server.errors[0], RuntimeError), (
            "the captured failure must retain its application exception type"
        )
        server.pop_errors()


@pytest.mark.asyncio
async def test_serve_task_failure_is_reported_during_teardown() -> None:
    """Surface serve-task exceptions when the live context exits."""
    adapter = _FailingServeAdapter()

    async def run_server() -> None:
        async with LiveWebSocketServer(_make_app(), adapter=adapter):
            adapter.fail()
            done, _pending = await asyncio.wait({adapter.task}, timeout=1.0)
            assert adapter.task in done, "the failing serve task must finish promptly"

    with pytest.raises(LiveServerError) as caught:
        await run_server()

    assert any(
        isinstance(error, RuntimeError) and str(error) == "serve task failed"
        for error in caught.value.exceptions
    ), "teardown must report the serve-task failure"


@pytest.mark.asyncio
async def test_body_exception_is_preserved_with_server_error_note() -> None:
    """Keep a test-body failure primary and annotate it with server failures."""
    failure = RuntimeError("server failure")
    body_failure = ValueError("body failed")

    async def run_server() -> None:
        async with LiveWebSocketServer(_make_app(), adapter=_FakeAdapter()) as server:
            server.record_error(failure)
            raise body_failure

    with pytest.raises(ValueError, match="body failed") as caught:
        await run_server()

    assert any("server failure" in note for note in caught.value.__notes__), (
        "the body exception must include a note for the server failure"
    )


@pytest.mark.asyncio
async def test_http_responder_failure_is_captured_and_returns_generic_500() -> None:
    """Return generic HTTP failure details and retain the application error."""

    async def request_failure() -> None:
        async with LiveWebSocketServer(_make_app()) as server:
            port = int(server.base_url.rsplit(":", maxsplit=1)[1])
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.write(
                b"GET /fail HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n"
            )
            await writer.drain()
            response = await server.wait_for(reader.read(), timeout=2.0)
            writer.close()
            await writer.wait_closed()

            assert response.startswith(b"HTTP/1.1 500"), (
                "captured HTTP failures must return a generic 500 response"
            )
            assert b"Internal Server Error" in response, (
                "HTTP responses must not expose the application exception"
            )
            assert len(server.errors) == 1, (
                "the HTTP responder failure must be captured"
            )
            assert isinstance(server.errors[0], RuntimeError), (
                "the captured HTTP failure must retain its exception type"
            )

    with pytest.raises(LiveServerError) as caught:
        await request_failure()

    assert any(isinstance(error, RuntimeError) for error in caught.value.exceptions), (
        "unacknowledged HTTP failures must be raised at context exit"
    )


@pytest.mark.asyncio
async def test_uvicorn_import_failure_raises_missing_dependency_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Give a useful test-extra hint when the lazy Uvicorn import fails."""
    import falcon_pachinko.testing._uvicorn as uvicorn_adapter

    import_module = uvicorn_adapter.importlib.import_module

    def fail_uvicorn_import(name: str, package: str | None = None) -> object:
        if name == "uvicorn":
            msg = "No module named 'uvicorn'"
            raise ModuleNotFoundError(msg)
        return import_module(name, package)

    monkeypatch.setattr(uvicorn_adapter.importlib, "import_module", fail_uvicorn_import)
    adapter = uvicorn_adapter._UvicornAdapter(shutdown_timeout=0.1)
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)

    with pytest.raises(MissingDependencyError, match=r"falcon-pachinko\[testing\]"):
        await adapter.start(_make_app(), sock)

    with pytest.raises(MissingDependencyError):
        await adapter.task

    sock.close()
