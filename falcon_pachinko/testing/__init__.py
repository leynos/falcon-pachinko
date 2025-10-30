"""Testing utilities for exercising websocket integrations."""

from __future__ import annotations

import typing as typ

from falcon_pachinko import _testing_harness as _testing_harness

from ._common import MissingDependencyError
from .client import TraceEvent, WebSocketTestClient
from .harness import SimulatorConnection, SimulatorRouterHarness
from .simulator import WebSocketSimulator

websocket_simulator: typ.Any = getattr(_testing_harness, "websocket_simulator", None)

__all__ = [
    "MissingDependencyError",
    "SimulatorConnection",
    "SimulatorRouterHarness",
    "TraceEvent",
    "WebSocketSimulator",
    "WebSocketTestClient",
]
