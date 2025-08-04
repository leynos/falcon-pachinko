"""falcon-pachinko package."""

from __future__ import annotations

from .handlers import handles_message
from .protocols import WebSocketLike
from .resource import WebSocketResource
from .router import WebSocketRouter
from .websocket import WebSocketConnectionManager, install
from .workers import WorkerController, worker

__all__ = (
    "WebSocketConnectionManager",
    "WebSocketLike",
    "WebSocketResource",
    "WebSocketRouter",
    "WorkerController",
    "handles_message",
    "install",
    "worker",
)
