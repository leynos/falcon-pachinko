"""falcon-pachinko package."""

from __future__ import annotations

from .websocket import WebSocketConnectionManager, install


def hello() -> str:
    """
    Returns a friendly greeting message.
    
    Returns:
        A string containing a greeting.
    """
    return "hello from Python"


__all__ = ["hello", "install", "WebSocketConnectionManager"]
