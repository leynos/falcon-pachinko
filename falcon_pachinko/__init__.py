"""falcon-pachinko package."""

from __future__ import annotations

from .websocket import WebSocketConnectionManager, install


__all__ = ["install", "WebSocketConnectionManager"]
