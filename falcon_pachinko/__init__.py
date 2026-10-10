"""falcon-pachinko package."""

from __future__ import annotations

from .di import ServiceContainer, ServiceNotFoundError
from .handlers import handles_message
from .hooks import HookCollection, HookContext, HookManager
from .media import configure_raw_frame_media
from .protocols import WebSocketLike
from .resource import WebSocketResource
from .router import ResourceFactory, WebSocketRouter
from .testing import (
    MissingDependencyError,
    SimulatorConnection,
    SimulatorRouterHarness,
    TraceEvent,
    WebSocketSimulator,
    WebSocketTestClient,
)
from .websocket import (
    ConnectionBackend,
    InProcessBackend,
    WebSocketConnectionManager,
    install,
)
from .workers import WorkerController, worker

__all__ = (
    "ConnectionBackend",
    "HookCollection",
    "HookContext",
    "HookManager",
    "InProcessBackend",
    "MissingDependencyError",
    "ResourceFactory",
    "ServiceContainer",
    "ServiceNotFoundError",
    "SimulatorConnection",
    "SimulatorRouterHarness",
    "TraceEvent",
    "WebSocketConnectionManager",
    "WebSocketLike",
    "WebSocketResource",
    "WebSocketRouter",
    "WebSocketSimulator",
    "WebSocketTestClient",
    "WorkerController",
    "configure_raw_frame_media",
    "handles_message",
    "install",
    "worker",
)
