"""Unit tests for the WebSocketConnectionManager."""

from __future__ import annotations

import pytest
import pytest_asyncio

from falcon_pachinko.websocket import (
    ConnectionBackend,
    InProcessBackend,
    WebSocketConnectionManager,
    WebSocketConnectionNotFoundError,
)
from tests._stubs import RecordingBackend, RecordingWebSocket


class ErrorWebSocket(RecordingWebSocket):
    """WebSocket stub whose send raises an error."""

    async def send_media(self, data: object) -> None:
        """Raise to simulate a broken connection."""
        raise RuntimeError("boom")


async def _lobby_manager(
    ws1: RecordingWebSocket, ws2: RecordingWebSocket
) -> WebSocketConnectionManager:
    """Return a manager with ``ws1``/``ws2`` joined to the lobby as "a"/"b"."""
    mgr = WebSocketConnectionManager()
    await mgr.add_connection("a", ws1)
    await mgr.add_connection("b", ws2)
    await mgr.join_room("a", "lobby")
    await mgr.join_room("b", "lobby")
    return mgr


@pytest_asyncio.fixture
async def room_with_two_connections() -> tuple[
    WebSocketConnectionManager, RecordingWebSocket, RecordingWebSocket
]:
    """Return a lobby with two connected websockets."""
    ws1 = RecordingWebSocket()
    ws2 = RecordingWebSocket()
    mgr = await _lobby_manager(ws1, ws2)
    return mgr, ws1, ws2


async def corrupt_room_membership(
    mgr: WebSocketConnectionManager, room: str, ghost_id: str
) -> None:
    """Inject an unknown connection ID into a room for testing."""
    backend = mgr.backend
    assert isinstance(backend, InProcessBackend), (
        "this helper can only corrupt the in-process backend"
    )
    async with backend._lock:  # pragma: no cover - internal test helper
        backend._rooms.setdefault(room, set()).add(ghost_id)


@pytest.mark.asyncio
async def test_send_to_connection_sends_message() -> None:
    """Send a message to a single connection."""
    mgr = WebSocketConnectionManager()
    ws = RecordingWebSocket()
    await mgr.add_connection("a", ws)

    await mgr.send_to_connection("a", {"hello": "world"})

    assert ws.messages == [{"hello": "world"}], (
        "the target connection must receive the sent message"
    )


@pytest.mark.asyncio
async def test_send_to_connection_propagates_error() -> None:
    """Errors raised by send_media bubble up."""
    mgr = WebSocketConnectionManager()
    ws = ErrorWebSocket()
    await mgr.add_connection("a", ws)

    with pytest.raises(RuntimeError, match="boom"):
        await mgr.send_to_connection("a", "ping")


@pytest.mark.asyncio
async def test_add_connection_raises_on_duplicate_id() -> None:
    """Adding a duplicate connection ID fails."""
    mgr = WebSocketConnectionManager()
    ws1 = RecordingWebSocket()
    ws2 = RecordingWebSocket()
    await mgr.add_connection("a", ws1)

    with pytest.raises(ValueError, match="Duplicate connection ID"):
        await mgr.add_connection("a", ws2)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("exclude", "expected_ws1", "expected_ws2"),
    [
        (None, ["hi"], ["hi"]),
        ({"a"}, [], ["hi"]),
    ],
    ids=["no-exclusions", "exclude-a"],
)
async def test_broadcast_to_room_with_exclusion_scenarios(
    room_with_two_connections: tuple[
        WebSocketConnectionManager, RecordingWebSocket, RecordingWebSocket
    ],
    exclude: set[str] | None,
    expected_ws1: list[str],
    expected_ws2: list[str],
) -> None:
    """Test broadcasting to room with various exclusion scenarios."""
    mgr, ws1, ws2 = room_with_two_connections

    await mgr.broadcast_to_room("lobby", "hi", exclude=exclude)

    assert ws1.messages == expected_ws1, (
        f"ws1 should have received {expected_ws1!r}, got {ws1.messages!r}"
    )
    assert ws2.messages == expected_ws2, (
        f"ws2 should have received {expected_ws2!r}, got {ws2.messages!r}"
    )


@pytest.mark.asyncio
async def test_broadcast_to_room_propagates_error() -> None:
    """Broadcasting propagates errors from any connection."""
    mgr = await _lobby_manager(RecordingWebSocket(), ErrorWebSocket())

    with pytest.raises(RuntimeError, match="boom"):
        await mgr.broadcast_to_room("lobby", 42)


@pytest.mark.asyncio
async def test_broadcast_to_room_aggregates_multiple_errors() -> None:
    """Aggregates exceptions when several sends fail."""
    mgr = await _lobby_manager(ErrorWebSocket(), ErrorWebSocket())

    with pytest.raises(ExceptionGroup) as excinfo:
        await mgr.broadcast_to_room("lobby", 42)

    assert len(excinfo.value.exceptions) == 2, (
        "both connection failures must be aggregated into the exception group"
    )


@pytest.mark.asyncio
async def test_join_room_requires_known_connection() -> None:
    """Joining a room with an unknown connection raises an error."""
    mgr = WebSocketConnectionManager()

    with pytest.raises(WebSocketConnectionNotFoundError):
        await mgr.join_room("ghost", "lobby")


@pytest.mark.asyncio
async def test_send_to_unknown_connection_raises_key_error() -> None:
    """Sending to an unknown connection raises a not-found error.

    WebSocketConnectionNotFoundError is a KeyError subclass.
    """
    mgr = WebSocketConnectionManager()

    with pytest.raises(WebSocketConnectionNotFoundError):
        await mgr.send_to_connection("a", "hi")


@pytest.mark.asyncio
async def test_connections_handle_room_filters(
    room_with_two_connections: tuple[
        WebSocketConnectionManager, RecordingWebSocket, RecordingWebSocket
    ],
) -> None:
    """Iterating yields all connections, room members, or nothing for empty rooms."""
    mgr, ws1, ws2 = room_with_two_connections

    assert {ws async for ws in mgr.connections()} == {ws1, ws2}, (
        "iterating without a room must yield every connection"
    )
    assert {ws async for ws in mgr.connections(room="lobby")} == {ws1, ws2}, (
        "iterating the lobby room must yield its members"
    )
    assert [ws async for ws in mgr.connections(room="ghost")] == [], (
        "iterating an unknown room must yield nothing"
    )


@pytest.mark.asyncio
async def test_connections_iterates_room_with_exclusion(
    room_with_two_connections: tuple[
        WebSocketConnectionManager, RecordingWebSocket, RecordingWebSocket
    ],
) -> None:
    """Iterating a room honours the exclusion list."""
    mgr, _, ws2 = room_with_two_connections

    seen = [ws async for ws in mgr.connections(room="lobby", exclude={"a"})]

    assert seen == [ws2], "the excluded connection must not be yielded"


@pytest.mark.asyncio
async def test_connections_ignore_unknown_ids_in_exclude(
    room_with_two_connections: tuple[
        WebSocketConnectionManager, RecordingWebSocket, RecordingWebSocket
    ],
) -> None:
    """Unknown IDs in ``exclude`` are ignored."""
    mgr, ws1, ws2 = room_with_two_connections

    seen = [ws async for ws in mgr.connections(room="lobby", exclude={"ghost"})]

    assert set(seen) == {ws1, ws2}, "an unknown excluded ID must not filter anything"


@pytest.mark.asyncio
async def test_connections_skip_stale_room_member(
    room_with_two_connections: tuple[
        WebSocketConnectionManager, RecordingWebSocket, RecordingWebSocket
    ],
) -> None:
    """Iterating a corrupted room skips ghost memberships."""
    mgr, ws1, ws2 = room_with_two_connections

    await corrupt_room_membership(mgr, "lobby", "ghost")

    seen = [ws async for ws in mgr.connections(room="lobby")]

    assert set(seen) == {ws1, ws2}, "the ghost membership must be skipped"


@pytest.mark.asyncio
async def test_broadcast_to_room_skips_stale_members(
    room_with_two_connections: tuple[
        WebSocketConnectionManager, RecordingWebSocket, RecordingWebSocket
    ],
) -> None:
    """Broadcasting ignores ghost memberships injected into the backend."""
    mgr, ws1, ws2 = room_with_two_connections

    await corrupt_room_membership(mgr, "lobby", "ghost")

    await mgr.broadcast_to_room("lobby", "hi")

    assert ws1.messages == ["hi"], "ws1 must still receive the broadcast"
    assert ws2.messages == ["hi"], "ws2 must still receive the broadcast"


@pytest.mark.asyncio
async def test_websockets_property_returns_snapshot() -> None:
    """Exposing websockets returns a stable snapshot."""
    mgr = WebSocketConnectionManager()
    ws = RecordingWebSocket()
    await mgr.add_connection("a", ws)
    snapshot = mgr.websockets
    await mgr.add_connection("b", RecordingWebSocket())
    assert dict(snapshot) == {"a": ws}, (
        "the snapshot must not reflect connections added afterwards"
    )


def test_default_backend_is_inprocess() -> None:
    """Ensure the default backend is used."""
    mgr = WebSocketConnectionManager()
    assert isinstance(mgr.backend, InProcessBackend), (
        "the default backend must be InProcessBackend"
    )
    assert isinstance(mgr.backend, ConnectionBackend), (
        "the default backend must implement ConnectionBackend"
    )


@pytest.mark.asyncio
async def test_manager_uses_custom_backend() -> None:
    """Custom backends should drive storage and broadcasts."""
    backend = RecordingBackend()
    mgr = WebSocketConnectionManager(backend=backend)
    ws = RecordingWebSocket()

    await mgr.add_connection("alice", ws)
    await mgr.join_room("alice", "crew")
    await mgr.broadcast_to_room("crew", {"msg": "hi"})
    await mgr.send_to_connection("alice", {"msg": "direct"})

    assert backend.calls == [
        "add_connection:alice",
        "join_room:alice:crew",
        "snapshot:crew",
        "get_websocket:alice",
    ], "the custom backend must receive calls in the expected order"
    assert ws.messages == [{"msg": "hi"}, {"msg": "direct"}], (
        "the websocket must receive both the broadcast and the direct message"
    )
    assert backend.rooms == {"crew": {"alice"}}, (
        "the custom backend must report the crew room membership"
    )
