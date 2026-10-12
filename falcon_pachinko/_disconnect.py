"""Framework-independent WebSocket disconnect signal."""

from __future__ import annotations


class _PeerDisconnectedError(Exception):
    """Signal that the peer ended a session, preserving its close code."""

    def __init__(self, code: int | None = None) -> None:
        """Create a disconnect signal with an optional peer close code."""
        super().__init__(code)
        self.code = code
