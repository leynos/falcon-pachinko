"""Testing utilities for exercising websocket integrations."""

from __future__ import annotations

from ._common import MissingDependencyError
from .client import (
    TraceEvent,
    WebSocketSession,
    WebSocketTestClient,
    WebSocketTestSession,
)
from .fanout import InMemoryFanoutTransport
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
    "InMemoryFanoutTransport",
    "LiveServerError",
    "LiveServerShutdownError",
    "LiveServerStartupError",
    "LiveWebSocketServer",
    "MissingDependencyError",
    "ServerAdapter",
    "SimulatorConnection",
    "SimulatorRouterHarness",
    "TraceEvent",
    "WebSocketSession",
    "WebSocketSimulator",
    "WebSocketTestClient",
    "WebSocketTestSession",
    "live_websocket_server",
    "websocket_simulator",
]
