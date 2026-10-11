"""Listener termination retains transport errors and stops renewal tasks."""

from __future__ import annotations

import asyncio
import typing as typ
from unittest import mock

import pytest

from falcon_pachinko import ClusterFanout, FanoutMessage, WebSocketConnectionManager
from falcon_pachinko.testing import InMemoryFanoutTransport

if typ.TYPE_CHECKING:
    import collections.abc as cabc


@pytest.mark.asyncio
@pytest.mark.parametrize("fails", [False, True])
async def test_listener_exit_stops_heartbeat_and_preserves_error(
    *, fails: bool
) -> None:
    """Clean EOF and transport failures both release the node exactly once."""
    transport = InMemoryFanoutTransport()
    node = ClusterFanout("node", WebSocketConnectionManager(), transport, lease_ttl=1)
    failure = ConnectionError("listener transport failed")
    heartbeat_started = asyncio.Event()
    heartbeat_stopped = asyncio.Event()

    async def heartbeat() -> None:
        """Expose cancellation of the running renewal task."""
        heartbeat_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            heartbeat_stopped.set()

    async def listen(node_id: str) -> cabc.AsyncIterator[bytes]:
        """End only after the renewal task is active."""
        await heartbeat_started.wait()
        if fails:
            raise failure
        yield FanoutMessage("id", node_id, "empty-room", None).encode()

    node._heartbeat = mock.AsyncMock(side_effect=heartbeat)
    transport.listen = mock.Mock(side_effect=listen)
    transport.release = mock.AsyncMock(wraps=transport.release)
    async with asyncio.timeout(1):
        if fails:
            with pytest.raises(ConnectionError) as caught:
                await node.run()
            assert caught.value is failure, "retain the original transport exception"
        else:
            await node.run()
    assert heartbeat_stopped.is_set(), "listener exit must stop lease renewal"
    transport.release.assert_awaited_once_with("node")


def test_envelope_wire_snapshot() -> None:
    """Freeze the serializable wire representation independently of decoding."""
    envelope = (
        b'{"message_id":"fixed","origin_node":"a","room":"lobby",'
        b'"payload":{"text":"hello"},"exclude":["b"]}'
    )
    message = FanoutMessage("fixed", "a", "lobby", {"text": "hello"}, ("b",))
    assert message.encode() == envelope, "wire fields and encoding must remain stable"
    assert FanoutMessage.decode(envelope) == message, "decode the fixed wire fixture"
