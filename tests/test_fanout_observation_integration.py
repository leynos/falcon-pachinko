"""Operational events distinguish decoding, delivery, and worker shutdown."""

from __future__ import annotations

import asyncio
import logging

import pytest

from falcon_pachinko import ClusterFanout, WebSocketConnectionManager
from falcon_pachinko.testing import InMemoryFanoutTransport
from tests._stubs import RecordingWebSocket


class _RecoveringSocket(RecordingWebSocket):
    """Fail one delivery with sensitive text, then acknowledge recovery."""

    def __init__(self) -> None:
        """Track the injected first-send failure and later delivery completion."""
        super().__init__()
        self.received = asyncio.Event()
        self.has_failed = False

    async def send_media(self, data: object) -> None:
        """Exercise a failed send without exposing its exception value."""
        if not self.has_failed:
            self.has_failed = True
            msg = "secret exception value"
            raise RuntimeError(msg)
        await super().send_media(data)
        self.received.set()


@pytest.mark.asyncio
async def test_coordinator_observations_are_safe_and_distinguish_failures(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Frequent successful deliveries are silent; failures retain their phase."""
    caplog.set_level(logging.DEBUG, logger="falcon_pachinko.fanout")
    transport = InMemoryFanoutTransport()
    node = ClusterFanout(
        "secret-node", WebSocketConnectionManager(), transport, lease_ttl=30
    )
    socket = _RecoveringSocket()
    await node.add_connection("secret-connection", socket)
    await node.join_room("secret-connection", "secret-room")
    task = asyncio.create_task(node.run())
    try:
        await transport.publish("secret-room", b"secret malformed envelope")
        await node.publish("secret-room", "secret-payload")
        await node.publish("secret-room", "recovered")
        async with asyncio.timeout(1):
            await socket.received.wait()
        await node.remove_connection("secret-connection")
    finally:
        task.cancel()
        async with asyncio.timeout(1):
            with pytest.raises(asyncio.CancelledError):
                await task

    events = [record for record in caplog.records if hasattr(record, "operation")]
    outcomes = {
        (vars(record)["operation"], vars(record)["outcome"]) for record in events
    }
    assert {("decode", "failed"), ("deliver", "failed")} <= outcomes, (
        "malformed envelopes and failed local sends need distinct observations"
    )
    assert {("publish", "succeeded"), ("subscribe", "succeeded")} <= outcomes, (
        "publication and subscription must expose their outcomes"
    )
    assert ("refresh", "succeeded") in outcomes, "lease renewal must be observable"
    assert {("unsubscribe", "succeeded"), ("release", "succeeded")} <= outcomes, (
        "membership cleanup and worker release must expose their outcomes"
    )
    assert ("listen", "cancelled") in outcomes, "classify worker cancellation"
    assert ("deliver", "succeeded") not in outcomes, "normal deliveries remain silent"
    assert all("secret" not in str(record.__dict__) for record in events), (
        "operation observations must omit identifiers, payloads, and exception text"
    )
