"""Contracts for serializable cluster delivery and local socket ownership."""

from __future__ import annotations

import asyncio
from unittest import mock

import pytest

from falcon_pachinko import ClusterFanout, FanoutMessage, WebSocketConnectionManager
from falcon_pachinko.testing import InMemoryFanoutTransport
from tests._fanout_stubs import _FailOnceSocket, _NotifyingSocket
from tests._stubs import RecordingWebSocket


@pytest.mark.asyncio
async def test_first_join_last_leave_and_disconnect_cleanup() -> None:
    """Only rooms containing local connections retain a node lease."""
    transport = InMemoryFanoutTransport()
    manager = WebSocketConnectionManager()
    node = ClusterFanout("a", manager, transport, lease_ttl=30.0)
    await node.add_connection("one", RecordingWebSocket())
    await node.add_connection("two", RecordingWebSocket())
    subscribe = mock.AsyncMock(wraps=transport.subscribe)
    transport.subscribe = mock.AsyncMock(side_effect=subscribe)
    await node.join_room("one", "room")
    await node.join_room("two", "room")
    subscribe.assert_awaited_once_with("room", "a", ttl=30.0)
    assert await transport.members("room") == ("a",), (
        "the live node must retain its room lease"
    )
    await node.leave_room("one", "room")
    assert await transport.members("room") == ("a",), (
        "the live node must retain its room lease"
    )
    await node.remove_connection("two")
    assert await transport.members("room") == (), (
        "the inactive room must have no node leases"
    )


@pytest.mark.asyncio
async def test_serialization_fails_before_publish() -> None:
    """Invalid application objects cannot cross the transport boundary."""
    transport = InMemoryFanoutTransport()
    node = ClusterFanout("a", WebSocketConnectionManager(), transport, lease_ttl=30.0)
    publish = mock.AsyncMock(wraps=transport.publish)
    transport.publish = publish
    with pytest.raises(TypeError):
        await node.publish("room", object())
    publish.assert_not_awaited()


@pytest.mark.asyncio
async def test_listener_excludes_ids_and_releases_on_cancellation() -> None:
    """A receiving node resolves only its own sockets and exclusion IDs."""
    transport = InMemoryFanoutTransport()
    manager = WebSocketConnectionManager()
    node = ClusterFanout("a", manager, transport, lease_ttl=30.0)
    included, excluded = _NotifyingSocket(), RecordingWebSocket()
    await node.add_connection("one", included)
    await node.add_connection("two", excluded)
    await node.join_room("one", "room")
    await node.join_room("two", "room")
    task = asyncio.create_task(node.run())
    try:
        await node.publish("room", {"hello": "world"}, exclude=("two",))
        async with asyncio.timeout(1):
            await included.sent.wait()
        assert not excluded.messages, "the excluded connection must receive nothing"
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert await transport.members("room") == (), (
        "the inactive room must have no node leases"
    )


@pytest.mark.asyncio
async def test_lease_expiry_and_renewal() -> None:
    """A crashed node expires while a refreshed node retains membership."""
    now = [0.0]
    transport = InMemoryFanoutTransport(clock=lambda: now[0])
    await transport.subscribe("room", "a", ttl=3.0)
    await transport.subscribe("room", "b", ttl=3.0)
    now[0] = 2.0
    await transport.refresh("a", ttl=3.0)
    now[0] = 4.0
    assert await transport.members("room") == ("a",), (
        "the live node must retain its room lease"
    )
    now[0] = 5.0
    assert await transport.members("room") == (), (
        "the inactive room must have no node leases"
    )


def test_envelope_round_trip() -> None:
    """JSON envelopes retain identifiers and application payloads."""
    message = FanoutMessage("id", "a", "room", {"value": 1}, ("one",))
    assert FanoutMessage.decode(message.encode()) == message, (
        "envelope fields must round trip"
    )


@pytest.mark.asyncio
async def test_listener_survives_failed_send() -> None:
    """A failed recipient stays registered and receives later messages."""
    transport = InMemoryFanoutTransport()
    manager = WebSocketConnectionManager()
    node = ClusterFanout("a", manager, transport, lease_ttl=30.0)
    socket = _FailOnceSocket()
    await node.add_connection("one", socket)
    await node.join_room("one", "room")
    task = asyncio.create_task(node.run())
    try:
        await node.publish("room", "first")
        await node.publish("room", "second")
        async with asyncio.timeout(1):
            await socket.sent.wait()
        assert socket.messages == ["second"], (
            "later messages must survive a send failure"
        )
        assert "one" in manager.websockets, (
            "delivery failure must not evict a local socket"
        )
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
async def test_transport_rejects_non_bytes() -> None:
    """The test adapter enforces the envelope boundary at runtime."""
    transport = InMemoryFanoutTransport()
    with pytest.raises(TypeError, match="must be bytes"):
        await transport.publish("room", "invalid")  # ty: ignore[invalid-argument-type] - exercise runtime boundary


@pytest.mark.asyncio
async def test_restart_resubscribes_existing_local_rooms() -> None:
    """Restarting a stopped worker restores leases without reconnecting sockets."""
    transport = InMemoryFanoutTransport()
    node = ClusterFanout("a", WebSocketConnectionManager(), transport, lease_ttl=30.0)
    socket = _NotifyingSocket()
    await node.add_connection("one", socket)
    await node.join_room("one", "room")
    first = asyncio.create_task(node.run())
    await node.publish("room", "first")
    async with asyncio.timeout(1):
        await socket.sent.wait()
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    assert not await transport.members("room"), "shutdown must release the old lease"
    socket.sent.clear()
    renewed = asyncio.Event()
    original_subscribe = transport.subscribe

    async def subscribe(room: str, node_id: str, *, ttl: float) -> None:
        """Signal after the restarted worker reacquires its room lease."""
        await original_subscribe(room, node_id, ttl=ttl)
        renewed.set()

    transport.subscribe = mock.AsyncMock(side_effect=subscribe)
    second = asyncio.create_task(node.run())
    try:
        async with asyncio.timeout(1):
            await renewed.wait()
        await node.publish("room", "second")
        async with asyncio.timeout(1):
            await socket.sent.wait()
        assert socket.messages == ["first", "second"], (
            "the restarted worker must deliver"
        )
    finally:
        second.cancel()
        with pytest.raises(asyncio.CancelledError):
            await second


@pytest.mark.asyncio
async def test_heartbeat_reacquires_expired_local_room_lease() -> None:
    """An event-loop stall must not permanently orphan occupied local rooms."""
    now = [0.0]
    transport = InMemoryFanoutTransport(clock=lambda: now[0])
    node = ClusterFanout(
        "a",
        WebSocketConnectionManager(),
        transport,
        lease_ttl=1.0,
        heartbeat_interval=0.01,
    )
    await node.add_connection("one", RecordingWebSocket())
    await node.join_room("one", "room")
    refreshed = asyncio.Event()
    original_refresh = transport.refresh

    async def refresh(node_id: str, *, ttl: float) -> None:
        """Signal when the heartbeat refresh reaches the transport."""
        await original_refresh(node_id, ttl=ttl)
        refreshed.set()

    transport.refresh = mock.AsyncMock(side_effect=refresh)
    task = asyncio.create_task(node.run())
    try:
        async with asyncio.timeout(1):
            await refreshed.wait()
        refreshed.clear()
        now[0] = 2.0
        assert not await transport.members("room"), "the stalled lease must expire"
        async with asyncio.timeout(1):
            await refreshed.wait()
        assert await transport.members("room") == ("a",), (
            "heartbeat must restore occupied rooms"
        )
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["leave", "remove"])
async def test_heartbeat_retries_failed_last_member_unsubscribe(operation: str) -> None:
    """A temporary transport failure must not preserve empty-room leases."""
    transport = InMemoryFanoutTransport()
    node = ClusterFanout(
        "a",
        WebSocketConnectionManager(),
        transport,
        lease_ttl=1.0,
        heartbeat_interval=0.01,
    )
    await node.add_connection("one", RecordingWebSocket())
    await node.join_room("one", "room")
    removed = asyncio.Event()
    original_unsubscribe = transport.unsubscribe
    has_failed = False

    async def unsubscribe(room: str, node_id: str) -> None:
        """Fail the first removal and signal when heartbeat retries it."""
        nonlocal has_failed
        if not has_failed:
            has_failed = True
            msg = "temporary unsubscribe failure"
            raise RuntimeError(msg)
        await original_unsubscribe(room, node_id)
        removed.set()

    transport.unsubscribe = mock.AsyncMock(side_effect=unsubscribe)
    action = (
        node.leave_room("one", "room")
        if operation == "leave"
        else node.remove_connection("one")
    )
    await action
    task = asyncio.create_task(node.run())
    try:
        async with asyncio.timeout(1):
            await removed.wait()
        assert not await transport.members("room"), (
            "heartbeat must remove the orphan lease"
        )
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
async def test_failed_unsubscribe_does_not_block_other_rooms_or_prolong_lease() -> None:
    """Pending failures expire while healthy rooms are removed or renewed."""
    now = [0.0]
    transport = InMemoryFanoutTransport(clock=lambda: now[0])
    node = ClusterFanout(
        "a",
        WebSocketConnectionManager(),
        transport,
        lease_ttl=1.0,
        heartbeat_interval=0.01,
    )
    await node.add_connection("departing", RecordingWebSocket())
    await node.add_connection("staying", RecordingWebSocket())
    await node.join_room("departing", "bad")
    await node.join_room("departing", "good")
    await node.join_room("staying", "occupied")
    original_unsubscribe = transport.unsubscribe
    renewed = asyncio.Event()
    original_subscribe = transport.subscribe

    async def unsubscribe(room: str, node_id: str) -> None:
        """Keep bad-room removal pending while allowing other cleanup."""
        if room == "bad":
            msg = "persistent room cleanup failure"
            raise RuntimeError(msg)
        await original_unsubscribe(room, node_id)

    async def subscribe(room: str, node_id: str, *, ttl: float) -> None:
        """Record an occupied-room renewal after pending removals are retried."""
        await original_subscribe(room, node_id, ttl=ttl)
        renewed.set()

    transport.unsubscribe = mock.AsyncMock(side_effect=unsubscribe)
    transport.subscribe = mock.AsyncMock(side_effect=subscribe)
    await node.remove_connection("departing")
    assert not await transport.members("good"), "one failed room must not block another"
    now[0] = 2.0
    task = asyncio.create_task(node.run())
    try:
        async with asyncio.timeout(1):
            await renewed.wait()
        assert not await transport.members("bad"), "a failed empty lease must expire"
        assert await transport.members("occupied") == ("a",), (
            "occupied rooms must renew"
        )
        assert not task.done(), "an unsubscribe failure must not stop the listener"
    finally:
        transport.unsubscribe = mock.AsyncMock(wraps=original_unsubscribe)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["refresh", "subscribe"])
async def test_heartbeat_recovers_from_transient_transport_failure(
    failure: str,
) -> None:
    """Temporary renewal errors must not cancel the local delivery listener."""
    transport = InMemoryFanoutTransport()
    node = ClusterFanout(
        "a",
        WebSocketConnectionManager(),
        transport,
        lease_ttl=1.0,
        heartbeat_interval=0.01,
    )
    socket = _NotifyingSocket()
    await node.add_connection("one", socket)
    await node.join_room("one", "room")
    recovered = asyncio.Event()
    calls = 0
    original_refresh = transport.refresh
    original_subscribe = transport.subscribe

    def renew() -> None:
        """Raise once, then signal that heartbeat retries have recovered."""
        nonlocal calls
        calls += 1
        if calls == 2:
            msg = "temporary heartbeat failure"
            raise RuntimeError(msg)
        if calls >= 3:
            recovered.set()

    async def refresh(node_id: str, *, ttl: float) -> None:
        """Apply the injected heartbeat failure before refreshing leases."""
        renew()
        await original_refresh(node_id, ttl=ttl)

    async def subscribe(room: str, node_id: str, *, ttl: float) -> None:
        """Apply the injected heartbeat failure before subscribing rooms."""
        renew()
        await original_subscribe(room, node_id, ttl=ttl)

    if failure == "refresh":
        transport.refresh = mock.AsyncMock(side_effect=refresh)
    else:
        transport.subscribe = mock.AsyncMock(side_effect=subscribe)
    task = asyncio.create_task(node.run())
    try:
        async with asyncio.timeout(1):
            await recovered.wait()
        assert not task.done(), (
            "a temporary heartbeat error must leave the listener running"
        )
        await node.publish("room", "after recovery")
        async with asyncio.timeout(1):
            await socket.sent.wait()
        assert socket.messages == ["after recovery"], (
            "delivery must continue after renewal recovery"
        )
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["raise", "stall"])
async def test_shutdown_release_is_bounded_and_preserves_cancellation(
    failure: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A broken broker release must not mask cancellation or hang shutdown."""
    caplog.set_level("DEBUG", logger="falcon_pachinko.fanout")
    transport = InMemoryFanoutTransport()
    node = ClusterFanout(
        "a",
        WebSocketConnectionManager(),
        transport,
        lease_ttl=0.03,
    )
    await node.add_connection("one", RecordingWebSocket())
    await node.join_room("one", "room")
    started = asyncio.Event()
    original_refresh = transport.refresh

    async def refresh(node_id: str, *, ttl: float) -> None:
        """Signal worker startup after membership refresh completes."""
        await original_refresh(node_id, ttl=ttl)
        started.set()

    async def release(node_id: str) -> None:
        """Raise or stall to exercise bounded worker cleanup."""
        if failure == "raise":
            msg = "broker release failed"
            raise RuntimeError(msg)
        await asyncio.Event().wait()

    transport.refresh = mock.AsyncMock(side_effect=refresh)
    transport.release = mock.AsyncMock(side_effect=release)
    task = asyncio.create_task(node.run())
    try:
        async with asyncio.timeout(1):
            await started.wait()
        task.cancel()
        done, _ = await asyncio.wait((task,), timeout=0.5)
        assert task in done, (
            "shutdown must finish within the lease-bounded grace period"
        )
        with pytest.raises(asyncio.CancelledError):
            await task
        assert any(
            vars(record).get("operation") == "release"
            and vars(record).get("outcome") == "failed"
            for record in caplog.records
        ), "release errors and expiry must be observable as operational failures"
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_shutdown_is_bounded_while_membership_holds_lock() -> None:
    """A stuck subscription must not prevent a cancelled worker from exiting."""
    transport = InMemoryFanoutTransport()
    node = ClusterFanout(
        "a",
        WebSocketConnectionManager(),
        transport,
        lease_ttl=0.03,
    )
    await node.add_connection("one", RecordingWebSocket())
    subscribing = asyncio.Event()

    async def subscribe(room: str, node_id: str, *, ttl: float) -> None:
        """Hold the membership lock until task cancellation interrupts it."""
        subscribing.set()
        await asyncio.Event().wait()

    transport.subscribe = mock.AsyncMock(side_effect=subscribe)
    join_task = asyncio.create_task(node.join_room("one", "room"))
    task = asyncio.create_task(node.run())
    try:
        async with asyncio.timeout(1):
            await subscribing.wait()
        task.cancel()
        done, _ = await asyncio.wait((task,), timeout=0.5)
        assert task in done, (
            "membership lock acquisition must share the shutdown deadline"
        )
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        task.cancel()
        join_task.cancel()
        await asyncio.gather(task, join_task, return_exceptions=True)
