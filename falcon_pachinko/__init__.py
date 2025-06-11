"""falcon-pachinko package."""

from __future__ import annotations

from .websocket import WebSocketConnectionManager, install


def hello() -> str:
    """Return a friendly greeting."""
    return "hello from Python"


__all__ = ["hello", "install", "WebSocketConnectionManager"]
