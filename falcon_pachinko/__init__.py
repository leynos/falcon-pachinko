"""WebSocket framework with public runtime, testing, and diagnostics APIs.

The package re-exports its primary connection, routing, hook, worker, and
testing interfaces. Diagnostic payload samples are available only through the
explicit :class:`DiagnosticSanitizer` API; framework diagnostics omit values by
default.
"""

from __future__ import annotations

from .di import ServiceContainer, ServiceNotFoundError
from .diagnostics import DEFAULT_SENSITIVE_KEYS, DiagnosticSanitizer
from .handlers import handles_message
from .hooks import HookCollection, HookContext, HookManager
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
    "DEFAULT_SENSITIVE_KEYS",
    "ConnectionBackend",
    "DiagnosticSanitizer",
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
    "handles_message",
    "install",
    "worker",
)
