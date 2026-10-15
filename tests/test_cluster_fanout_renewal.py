"""Room lease renewal failure behaviour."""

from __future__ import annotations

import asyncio
from unittest import mock

import pytest

from falcon_pachinko import ClusterFanout, WebSocketConnectionManager
from falcon_pachinko.testing import InMemoryFanoutTransport
from tests._fanout_stubs import _NotifyingSocket
from tests._stubs import RecordingWebSocket


class _RoomRenewalScenario:
    """Coordinate fake-clock room leases and injected transport failures."""

    def __init__(self) -> None:
        """Initialize leases, events, and controlled transport failures."""
        self.now = [0.0]
        self.transport = InMemoryFanoutTransport(clock=lambda: self.now[0])
        self.node = ClusterFanout(
            "a",
            WebSocketConnectionManager(),
            self.transport,
            lease_ttl=1.0,
            heartbeat_interval=0.05,
        )
        self.healthy_socket = _NotifyingSocket()
        self.bad_renewal_attempted = asyncio.Event()
        self.healthy_renewal_succeeded = asyncio.Event()
        self.startup_subscriptions: list[str] = []
        self.bad_renewal_times: list[float] = []
        self.healthy_renewal_times: list[float] = []
        self.should_fail_bad_renewal = False
        self.original_subscribe = self.transport.subscribe
        self.original_unsubscribe = self.transport.unsubscribe
        self.transport.subscribe = mock.AsyncMock(side_effect=self.subscribe)
        self.transport.unsubscribe = mock.AsyncMock(side_effect=self.unsubscribe)

    async def prepare(self) -> None:
        """Create bad, healthy, and stale memberships in insertion order."""
        await self.node.add_connection("bad-member", RecordingWebSocket())
        await self.node.add_connection("healthy-member", self.healthy_socket)
        await self.node.add_connection("stale-member", RecordingWebSocket())
        await self.node.join_room("bad-member", "bad")
        await self.node.join_room("healthy-member", "healthy")
        await self.node.join_room("stale-member", "stale")
        await self.node.remove_connection("stale-member")
        assert await self.transport.members("stale") == ("a",), (
            "a failed last-leave unsubscribe must remain leased until expiry"
        )
        self.startup_subscriptions.clear()

    async def subscribe(self, room: str, node_id: str, *, ttl: float) -> None:
        """Fail one room renewal and record whether the next room succeeds."""
        if self.should_fail_bad_renewal and room == "bad":
            self.bad_renewal_times.append(self.now[0])
            self.bad_renewal_attempted.set()
            msg = "bad room renewal failed"
            raise RuntimeError(msg)
        if self.should_fail_bad_renewal and room == "healthy":
            await self.original_subscribe(room, node_id, ttl=ttl)
            self.healthy_renewal_times.append(self.now[0])
            self.healthy_renewal_succeeded.set()
            return
        self.startup_subscriptions.append(room)
        await self.original_subscribe(room, node_id, ttl=ttl)

    async def unsubscribe(self, room: str, node_id: str) -> None:
        """Keep stale cleanup pending until the worker shuts down."""
        if room == "stale":
            msg = "persistent stale-room cleanup failure"
            raise RuntimeError(msg)
        await self.original_unsubscribe(room, node_id)

    async def confirm_startup(self) -> None:
        """Prove initial room renewal and listener delivery before injection."""
        await self.node.publish("healthy", "startup")
        async with asyncio.timeout(1):
            await self.healthy_socket.sent.wait()
        assert self.startup_subscriptions[:2] == ["bad", "healthy"], (
            "initial renewal must subscribe to occupied rooms in insertion order"
        )
        self.healthy_socket.sent.clear()
        self.healthy_socket.messages.clear()

    def enable_bad_renewal_failure(self) -> None:
        """Trigger the failure before either occupied room's initial lease expires."""
        self.should_fail_bad_renewal = True
        self.now[0] = 0.75

    async def confirm_heartbeat_attempts(self) -> None:
        """Require both room attempts while their original leases remain valid."""
        async with asyncio.timeout(1):
            await self.bad_renewal_attempted.wait()
            await self.healthy_renewal_succeeded.wait()
        assert self.bad_renewal_times[0] < 1.0, (
            "the failing renewal must be attempted before the original lease expires"
        )
        assert self.healthy_renewal_times[0] < 1.0, (
            "the later healthy renewal must run before its original lease expires"
        )

    async def assert_healthy_lease_survives(self, task: asyncio.Task[None]) -> None:
        """Check failed-room expiry, healthy renewal, and listener survival."""
        self.now[0] = 1.1
        assert await self.transport.members("bad") == (), (
            "the failed room renewal must let its old lease expire"
        )
        assert await self.transport.members("healthy") == ("a",), (
            "a later healthy room must renew despite an earlier room failure"
        )
        assert not task.done(), "room renewal failure must not stop the listener"

    async def stop(self, task: asyncio.Task[None]) -> None:
        """Restore shutdown transport behaviour and require bounded cancellation."""
        self.transport.unsubscribe = mock.AsyncMock(wraps=self.original_unsubscribe)
        task.cancel()
        async with asyncio.timeout(1):
            (outcome,) = await asyncio.gather(task, return_exceptions=True)
        assert isinstance(outcome, asyncio.CancelledError), (
            "listener shutdown must complete through cancellation"
        )


@pytest.mark.asyncio
async def test_room_renewal_failure_does_not_starve_healthy_rooms() -> None:
    """One occupied-room failure must not starve later leases or the listener."""
    scenario = _RoomRenewalScenario()
    await scenario.prepare()
    task = asyncio.create_task(scenario.node.run())
    try:
        await scenario.confirm_startup()
        scenario.enable_bad_renewal_failure()
        await scenario.confirm_heartbeat_attempts()
        await scenario.assert_healthy_lease_survives(task)
    finally:
        await scenario.stop(task)
