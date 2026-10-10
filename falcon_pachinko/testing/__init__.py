"""Testing utilities for exercising websocket integrations."""

from __future__ import annotations

from ._common import MissingDependencyError
from .client import TraceEvent, WebSocketTestClient
from .fixtures import live_websocket_server, websocket_simulator
from .harness import SimulatorConnection, SimulatorRouterHarness
from .live import (
    LiveServerError,
    LiveServerShutdownError,
    LiveServerStartupError,
    LiveWebSocketServer,
    ServerAdapter,
)
from .simulator import WebSocketSimulator

__all__ = [
    "LiveServerError",
    "LiveServerShutdownError",
    "LiveServerStartupError",
    "LiveWebSocketServer",
    "MissingDependencyError",
    "ServerAdapter",
    "SimulatorConnection",
    "SimulatorRouterHarness",
    "TraceEvent",
    "WebSocketSimulator",
    "WebSocketTestClient",
    "live_websocket_server",
    "websocket_simulator",
]
