"""Regression tests for read-only in-memory fan-out membership queries."""

from __future__ import annotations

import pytest

from falcon_pachinko.testing.fanout import InMemoryFanoutTransport


@pytest.mark.asyncio
async def test_members_returns_active_nodes_without_mutating_transport_state() -> None:
    """Inspection omits expired leases while retaining stored room state."""
    now = [0.0]
    transport = InMemoryFanoutTransport(clock=lambda: now[0])
    await transport.subscribe("lobby", "zulu", ttl=10.0)
    await transport.subscribe("lobby", "expired", ttl=1.0)
    await transport.subscribe("lobby", "alpha", ttl=10.0)
    await transport.subscribe("elsewhere", "other", ttl=10.0)

    rooms = transport._rooms
    room_members = dict(rooms)
    room_snapshot = {room: dict(members) for room, members in transport._rooms.items()}
    inboxes = transport._inboxes
    inbox_snapshot = dict(inboxes)
    now[0] = 2.0

    assert await transport.members("lobby") == ("alpha", "zulu"), (
        "queries must return sorted active memberships only"
    )

    assert transport._rooms is rooms, "inspection must retain the room mapping"
    assert transport._rooms == room_snapshot, "inspection must not prune stored leases"
    assert all(
        transport._rooms[room] is members for room, members in room_members.items()
    ), "inspection must retain each room's member mapping"
    assert transport._inboxes is inboxes, "inspection must retain the inbox mapping"
    assert transport._inboxes == inbox_snapshot, "inspection must not change inboxes"
    assert all(
        transport._inboxes[node_id] is inbox
        for node_id, inbox in inbox_snapshot.items()
    ), "inspection must retain every node's queue identity"
