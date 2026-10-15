"""Shared socket doubles for cluster fan-out unit tests."""

from __future__ import annotations

import asyncio

from tests._stubs import RecordingWebSocket


class _NotifyingSocket(RecordingWebSocket):
    """Signal completed sends without polling.

    Reuse the shared socket stub for lifecycle recording.
    """

    def __init__(self) -> None:
        """Create a socket stub that signals after each completed send."""
        super().__init__()
        self.sent = asyncio.Event()

    async def send_media(self, data: object) -> None:
        """Record the message and notify the waiting test."""
        await super().send_media(data)
        self.sent.set()


class _FailOnceSocket(_NotifyingSocket):
    """Fail one delivery then accept subsequent messages."""

    def __init__(self) -> None:
        """Arm the first-send failure used to test listener recovery."""
        super().__init__()
        self._failed = False

    async def send_media(self, data: object) -> None:
        """Exercise isolated ordinary send failures."""
        if not self._failed:
            self._failed = True
            msg = "first delivery fails"
            raise RuntimeError(msg)
        await super().send_media(data)
