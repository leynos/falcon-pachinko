"""Pytest fixtures published by the ``falcon_pachinko.testing`` plugin."""

from __future__ import annotations

import typing as typ

from .harness import SimulatorRouterHarness

if typ.TYPE_CHECKING:
    import collections.abc as cabc

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
