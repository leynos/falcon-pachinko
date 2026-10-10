"""Pytest fixtures published by the ``falcon_pachinko.testing`` plugin."""

from __future__ import annotations

import asyncio
import typing as typ

from .harness import SimulatorRouterHarness
from .live import LiveServerError, LiveWebSocketServer

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    import falcon.asgi

    _Result = typ.TypeVar("_Result")

    from .live import ServerAdapter

    class _LiveServerOptions(typ.TypedDict, total=False):
        """Optional configuration accepted by the live server fixture."""

        adapter: ServerAdapter | None
        startup_timeout: float
        shutdown_timeout: float
        capture_errors: bool


try:  # pragma: no cover - optional dependency for fixture registration
    import pytest
except ImportError:  # pragma: no cover - fixture only available under pytest
    pytest = None


def _websocket_simulator() -> cabc.Iterator[SimulatorRouterHarness]:
    """Provide a router harness pre-wired with a simulator factory.

    Yields
    ------
    SimulatorRouterHarness
        A freshly mounted harness. Any simulator staged for the next
        connection is discarded once the test completes.
    """
    harness = SimulatorRouterHarness()
    try:
        yield harness
    finally:
        harness.discard_pending_simulator()


# Registered as a pytest fixture when pytest is installed. The undecorated
# generator stays bound otherwise so that importing ``falcon_pachinko`` never
# requires pytest.
websocket_simulator = (
    _websocket_simulator if pytest is None else pytest.fixture(_websocket_simulator)
)


class _LiveServerRunner:
    """Drive live servers on one fresh event loop for a pytest test."""

    def __init__(self) -> None:
        self._loop = asyncio.new_event_loop()
        self._servers: list[LiveWebSocketServer] = []
        self._closed = False

    def start(
        self,
        app: falcon.asgi.App,
        **options: typ.Unpack[_LiveServerOptions],
    ) -> LiveWebSocketServer:
        """Start and track one live server on the runner's event loop."""
        server = LiveWebSocketServer(app, **options)
        # pylint: disable-next=unnecessary-dunder-call  # Keep the context open until fixture teardown.
        self.run(server.__aenter__())
        self._servers.append(server)
        return server

    def run(
        self, awaitable: cabc.Awaitable[_Result], timeout: float | None = None
    ) -> _Result:
        """Run an awaitable to completion, optionally enforcing a deadline."""
        if self._closed:
            msg = "the live server runner is already closed"
            raise RuntimeError(msg)
        if timeout is not None:
            awaitable = asyncio.wait_for(awaitable, timeout=timeout)
        return self._loop.run_until_complete(awaitable)

    def close(self) -> None:
        """Exit every started server, then close the per-test event loop."""
        if self._closed:
            return
        errors: list[Exception] = []
        try:
            for server in reversed(self._servers):
                try:
                    self.run(server.__aexit__(None, None, None))
                except LiveServerError as exc:
                    errors.extend(exc.exceptions)
                except Exception as exc:  # ruff: ignore[blind-except] -- preserve every server failure
                    errors.append(exc)
        finally:
            self._closed = True
            self._loop.close()
        if errors:
            raise LiveServerError(errors)


def _live_websocket_server() -> cabc.Iterator[_LiveServerRunner]:
    """Provide a loop-bound runner for real Falcon WebSocket servers."""
    runner = _LiveServerRunner()
    try:
        yield runner
    finally:
        runner.close()


live_websocket_server = (
    _live_websocket_server if pytest is None else pytest.fixture(_live_websocket_server)
)
