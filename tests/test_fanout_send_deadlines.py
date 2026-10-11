"""Local write backpressure must not indefinitely stall all node rooms."""

from __future__ import annotations

import asyncio

import pytest

from falcon_pachinko import ClusterFanout, WebSocketConnectionManager
from falcon_pachinko.testing import InMemoryFanoutTransport
from tests._fanout_stubs import _NotifyingSocket
from tests._stubs import RecordingWebSocket


class _StalledSocket(RecordingWebSocket):
    """Model a server write blocked by a peer that stopped reading."""

    async def send_media(self, data: object) -> None:
        """Remain blocked until the coordinator's per-send deadline cancels us."""
        await asyncio.Event().wait()


@pytest.mark.asyncio
async def test_default_send_deadline_keeps_other_rooms_delivering() -> None:
    """A queued healthy-room envelope survives an indefinitely stalled write."""
    transport = InMemoryFanoutTransport()
    node = ClusterFanout("a", WebSocketConnectionManager(), transport, lease_ttl=30)
    assert node.send_timeout == pytest.approx(5.0), (
        "the default local-send deadline is five seconds"
    )
    healthy = _NotifyingSocket()
    await node.add_connection("stalled", _StalledSocket())
    await node.add_connection("healthy", healthy)
    await node.join_room("stalled", "blocked")
    await node.join_room("healthy", "healthy")
    task = asyncio.create_task(node.run())
    try:
        await node.publish("blocked", "blocked write")
        await node.publish("healthy", "later envelope")
        async with asyncio.timeout(6):
            await healthy.sent.wait()
        assert healthy.messages == ["later envelope"], (
            "a stalled room must not indefinitely block the sequential listener"
        )
        assert "stalled" in node.manager.websockets, (
            "send timeout must not evict the local connection"
        )
    finally:
        task.cancel()
        async with asyncio.timeout(1):
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("timeout", [0.0, -1.0, float("inf"), float("nan")])
def test_send_deadline_rejects_invalid_bounds(timeout: float) -> None:
    """A numeric send deadline must be positive and finite."""
    with pytest.raises(ValueError, match="send_timeout"):
        ClusterFanout(
            "a",
            WebSocketConnectionManager(),
            InMemoryFanoutTransport(),
            lease_ttl=30,
            send_timeout=timeout,
        )


def test_explicit_none_retains_unbounded_send_opt_out() -> None:
    """Applications can deliberately choose no per-send deadline."""
    node = ClusterFanout(
        "a",
        WebSocketConnectionManager(),
        InMemoryFanoutTransport(),
        lease_ttl=30,
        send_timeout=None,
    )
    assert node.send_timeout is None, "explicit None must retain the unbounded opt-out"
