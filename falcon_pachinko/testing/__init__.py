"""Testing utilities for exercising websocket integrations."""

from __future__ import annotations

from ._common import MissingDependencyError
from .client import TraceEvent, WebSocketTestClient
from .fixtures import websocket_simulator as websocket_simulator
from .harness import SimulatorConnection, SimulatorRouterHarness
from .simulator import WebSocketSimulator

__all__ = [
    "MissingDependencyError",
    "SimulatorConnection",
    "SimulatorRouterHarness",
    "TraceEvent",
    "WebSocketSimulator",
    "WebSocketTestClient",
]
