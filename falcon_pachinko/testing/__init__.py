"""Testing utilities for exercising websocket integrations."""

from __future__ import annotations

import typing as typ

from ._common import MissingDependencyError
from .client import TraceEvent, WebSocketTestClient
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

try:  # pragma: no cover - optional dependency for fixture registration
    import pytest
except ImportError:  # pragma: no cover - fixture only available under pytest
    pytest = None  # type: ignore[assignment]
else:

    @pytest.fixture
    def websocket_simulator() -> typ.Iterator[SimulatorRouterHarness]:
        """Provide a router harness pre-wired with a simulator factory."""
        harness = SimulatorRouterHarness()
        try:
            yield harness
        finally:
            harness._pending_simulator = None
