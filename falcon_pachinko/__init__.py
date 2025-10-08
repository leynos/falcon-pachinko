"""falcon-pachinko package."""

from __future__ import annotations

from .di import ServiceContainer, ServiceNotFoundError
from .handlers import handles_message
from .hooks import HookCollection, HookContext, HookManager
from .protocols import WebSocketLike
from .resource import WebSocketResource
from .router import ResourceFactory, WebSocketRouter
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
    "ResourceFactory",
    "ServiceContainer",
    "ServiceNotFoundError",
    "WebSocketConnectionManager",
    "WebSocketLike",
    "WebSocketResource",
    "WebSocketRouter",
    "WorkerController",
    "handles_message",
    "install",
    "worker",
)
