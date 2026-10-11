"""Ambiguous lease acquisition must not leave an empty room renewed forever."""

from __future__ import annotations

import asyncio
import typing as typ

import pytest

from falcon_pachinko import ClusterFanout, WebSocketConnectionManager
from falcon_pachinko.testing import InMemoryFanoutTransport
from tests._stubs import RecordingWebSocket

if typ.TYPE_CHECKING:
    import collections.abc as cabc


class _PartialLeaseTransport(InMemoryFanoutTransport):
    """Acquire a real test lease before reporting an ambiguous failure."""

    def __init__(self, failure: BaseException) -> None:
        """Keep renewal timing explicit and signal the active listener."""
        self.now = 0.0
        self.failure = failure
        self.listening = asyncio.Event()
        super().__init__(clock=lambda: self.now)

    @typ.override
    async def subscribe(self, room: str, node_id: str, *, ttl: float) -> None:
        """Model a broker that applied the command before the caller failed."""
        await super().subscribe(room, node_id, ttl=ttl)
        raise self.failure

    @typ.override
    async def listen(self, node_id: str) -> cabc.AsyncIterator[bytes]:
        """Signal that initial lease maintenance has completed."""
        self.listening.set()
        async for envelope in super().listen(node_id):
            yield envelope


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_type", [RuntimeError, asyncio.CancelledError])
async def test_partial_first_subscribe_cannot_refresh_an_empty_room(
    failure_type: type[BaseException],
) -> None:
    """Both failed and cancelled acquisition retain idempotent remote cleanup."""
    failure = failure_type("lease applied before failure")
    transport = _PartialLeaseTransport(failure)
    node = ClusterFanout("a", WebSocketConnectionManager(), transport, lease_ttl=1.0)
    await node.add_connection("one", RecordingWebSocket())
    with pytest.raises(failure_type) as caught:
        await node.join_room("one", "room")
    assert caught.value is failure, "retain the original acquisition failure"
    assert not node.manager.rooms.get("room"), "rollback the only local membership"
    transport.now = 0.75
    task = asyncio.create_task(node.run())
    try:
        async with asyncio.timeout(1):
            await transport.listening.wait()
        transport.now = 1.1
        assert await transport.members("room") == (), (
            "maintenance must remove an ambiguous empty-room lease, never renew it"
        )
    finally:
        task.cancel()
        async with asyncio.timeout(1):
            await asyncio.gather(task, return_exceptions=True)
