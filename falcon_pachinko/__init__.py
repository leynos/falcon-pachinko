"""falcon-pachinko package."""

from __future__ import annotations

from .resource import WebSocketLike, WebSocketResource, handles_message
from .router import WebSocketRouter
from .websocket import WebSocketConnectionManager, install

__all__ = (
    "WebSocketConnectionManager",
    "WebSocketLike",
    "WebSocketResource",
    "WebSocketRouter",
    "handles_message",
    "install",
)
