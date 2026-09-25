"""Behavioural tests for the in-memory connection backend and manager."""

from __future__ import annotations

import types
import typing as typ

import pytest

from falcon_pachinko.websocket import (
    InProcessBackend,
    WebSocketConnectionManager,
    WebSocketConnectionNotFoundError,
)
from tests._stubs import RecordingWebSocket


@pytest.mark.asyncio
async def test_add_connection_raises_on_duplicate_id() -> None:
    """Registering the same connection ID twice raises ValueError."""
    backend = InProcessBackend()
    await backend.add_connection("a", RecordingWebSocket())

    with pytest.raises(ValueError, match="Duplicate connection ID"):
        await backend.add_connection("a", RecordingWebSocket())


@pytest.mark.asyncio
async def test_rooms_property_is_read_only_mapping_proxy() -> None:
    """The rooms property exposes a MappingProxyType that rejects writes."""
    backend = InProcessBackend()
    await backend.add_connection("a", RecordingWebSocket())
    await backend.join_room("a", "lobby")

    rooms = backend.rooms

    is_proxy = isinstance(rooms, types.MappingProxyType)
    assert is_proxy, "rooms must return a MappingProxyType"
    mutable_view = typ.cast("typ.Any", rooms)
    with pytest.raises(TypeError):
        mutable_view["lobby"] = frozenset()


@pytest.mark.asyncio
async def test_rooms_property_values_are_frozensets() -> None:
    """Each room in the snapshot is a frozenset of member IDs."""
    backend = InProcessBackend()
    await backend.add_connection("a", RecordingWebSocket())
    await backend.join_room("a", "lobby")

    members = backend.rooms["lobby"]

    assert isinstance(members, frozenset), "room membership must be a frozenset"
    assert members == frozenset({"a"}), "the lobby must contain only connection a"


@pytest.mark.asyncio
async def test_rooms_snapshot_is_isolated_from_later_mutation() -> None:
    """A prior rooms snapshot is unaffected by later backend mutation."""
    backend = InProcessBackend()
    await backend.add_connection("a", RecordingWebSocket())
    await backend.join_room("a", "lobby")

    earlier = backend.rooms
    await backend.add_connection("b", RecordingWebSocket())
    await backend.join_room("b", "lobby")

    unchanged = earlier["lobby"] == frozenset({"a"})
    assert unchanged, "an earlier snapshot must not see later membership changes"


@pytest.mark.asyncio
async def test_remove_connection_deletes_room_left_empty() -> None:
    """Removing the last member of a room deletes that room."""
    backend = InProcessBackend()
    await backend.add_connection("a", RecordingWebSocket())
    await backend.join_room("a", "lobby")

    await backend.remove_connection("a")

    assert "lobby" not in backend.rooms, "an emptied room must be removed"


@pytest.mark.asyncio
async def test_remove_connection_keeps_room_with_other_members() -> None:
    """Removing one of several members keeps the room with the rest."""
    backend = InProcessBackend()
    await backend.add_connection("a", RecordingWebSocket())
    await backend.add_connection("b", RecordingWebSocket())
    await backend.join_room("a", "lobby")
    await backend.join_room("b", "lobby")

    await backend.remove_connection("a")

    lobby = backend.rooms["lobby"]
    assert lobby == frozenset({"b"}), "the room must keep its remaining member"


@pytest.mark.asyncio
async def test_remove_connection_also_drops_the_websocket() -> None:
    """Removing a connection drops it from the websockets mapping too."""
    backend = InProcessBackend()
    await backend.add_connection("a", RecordingWebSocket())

    await backend.remove_connection("a")

    assert "a" not in backend.websockets, "the websocket entry must be removed"


@pytest.mark.asyncio
async def test_remove_connection_is_idempotent_for_unknown_id() -> None:
    """Removing an unknown connection ID does not raise."""
    backend = InProcessBackend()

    await backend.remove_connection("ghost")


@pytest.mark.asyncio
async def test_join_room_raises_for_unknown_connection() -> None:
    """Joining a room with an unregistered connection ID raises."""
    backend = InProcessBackend()

    with pytest.raises(WebSocketConnectionNotFoundError):
        await backend.join_room("ghost", "lobby")


@pytest.mark.asyncio
async def test_join_room_adds_known_connection() -> None:
    """Joining a room adds a registered connection to it."""
    backend = InProcessBackend()
    await backend.add_connection("a", RecordingWebSocket())

    await backend.join_room("a", "lobby")

    lobby = backend.rooms["lobby"]
    assert lobby == frozenset({"a"}), "the connection must appear in the joined room"


@pytest.mark.asyncio
async def test_leave_room_is_noop_for_unknown_room() -> None:
    """Leaving a room that does not exist does nothing."""
    backend = InProcessBackend()
    await backend.add_connection("a", RecordingWebSocket())

    await backend.leave_room("a", "ghost-room")

    assert "ghost-room" not in backend.rooms, "no room should be created"


@pytest.mark.asyncio
async def test_leave_room_removes_member_and_deletes_empty_room() -> None:
    """Leaving the last member of a room deletes that room."""
    backend = InProcessBackend()
    await backend.add_connection("a", RecordingWebSocket())
    await backend.join_room("a", "lobby")

    await backend.leave_room("a", "lobby")

    assert "lobby" not in backend.rooms, "the emptied room must be deleted"


@pytest.mark.asyncio
async def test_leave_room_keeps_room_with_remaining_members() -> None:
    """Leaving a room with other members present keeps that room."""
    backend = InProcessBackend()
    await backend.add_connection("a", RecordingWebSocket())
    await backend.add_connection("b", RecordingWebSocket())
    await backend.join_room("a", "lobby")
    await backend.join_room("b", "lobby")

    await backend.leave_room("a", "lobby")

    lobby = backend.rooms["lobby"]
    assert lobby == frozenset({"b"}), "the room must keep its remaining member"


@pytest.mark.asyncio
async def test_manager_rooms_property_delegates_to_backend() -> None:
    """The manager's rooms property forwards to the default backend."""
    mgr = WebSocketConnectionManager()
    await mgr.add_connection("a", RecordingWebSocket())

    await mgr.join_room("a", "lobby")

    lobby = mgr.rooms["lobby"]
    assert lobby == frozenset({"a"}), "the manager must report the room membership"


@pytest.mark.asyncio
async def test_manager_remove_connection_delegates_to_backend() -> None:
    """The manager's remove_connection forwards to the default backend."""
    mgr = WebSocketConnectionManager()
    await mgr.add_connection("a", RecordingWebSocket())
    await mgr.join_room("a", "lobby")

    await mgr.remove_connection("a")

    assert "lobby" not in mgr.rooms, "the emptied room must be removed"
    assert "a" not in mgr.websockets, "the removed connection must be forgotten"


@pytest.mark.asyncio
async def test_manager_join_room_delegates_to_backend() -> None:
    """The manager's join_room forwards to the default backend."""
    mgr = WebSocketConnectionManager()
    await mgr.add_connection("a", RecordingWebSocket())

    await mgr.join_room("a", "lobby")

    lobby = mgr.rooms["lobby"]
    assert lobby == frozenset({"a"}), "join_room must add the connection to the room"


@pytest.mark.asyncio
async def test_manager_leave_room_delegates_to_backend() -> None:
    """The manager's leave_room forwards to the default backend."""
    mgr = WebSocketConnectionManager()
    await mgr.add_connection("a", RecordingWebSocket())
    await mgr.join_room("a", "lobby")

    await mgr.leave_room("a", "lobby")

    assert "lobby" not in mgr.rooms, "leave_room must remove the emptied room"
