"""Unit tests for the WebSocketConnectionManager."""

from __future__ import annotations

import builtins
import types
import typing as typ

import pytest
import pytest_asyncio

from falcon_pachinko.websocket import (
    ConnectionBackend,
    InProcessBackend,
    WebSocketConnectionManager,
    WebSocketConnectionNotFoundError,
)

if typ.TYPE_CHECKING:
    from falcon_pachinko.protocols import WebSocketLike


class DummyWebSocket:
    """Minimal WebSocket stub that records sent messages."""

    def __init__(self) -> None:
        self.messages: list[object] = []

    async def accept(
        self, subprotocol: str | None = None
    ) -> None:  # pragma: no cover - not used
        """Accept the connection."""
        return

    async def close(self, code: int = 1000) -> None:  # pragma: no cover - not used
        """Close the connection."""
        return

    async def send_media(self, data: object) -> None:
        """Record a message sent via the stub."""
        self.messages.append(data)

    async def receive_media(self) -> object:  # pragma: no cover - unused
        """Return a placeholder payload."""
        return None


class ErrorWebSocket(DummyWebSocket):
    """WebSocket stub whose send raises an error."""

    async def send_media(
        self, data: object
    ) -> None:  # pragma: no cover - behaviour tested
        """Raise to simulate a broken connection."""
        raise RuntimeError("boom")


@pytest_asyncio.fixture
async def room_with_two_connections() -> tuple[
    WebSocketConnectionManager, DummyWebSocket, DummyWebSocket
]:
    """Return a lobby with two connected websockets."""
    mgr = WebSocketConnectionManager()
    ws1 = DummyWebSocket()
    ws2 = DummyWebSocket()
    await mgr.add_connection("a", ws1)
    await mgr.add_connection("b", ws2)
    await mgr.join_room("a", "lobby")
    await mgr.join_room("b", "lobby")
    return mgr, ws1, ws2


async def corrupt_room_membership(
    mgr: WebSocketConnectionManager, room: str, ghost_id: str
) -> None:
    """Inject an unknown connection ID into a room for testing."""
    backend = typ.cast("InProcessBackend", mgr.backend)
    async with backend._lock:  # pragma: no cover - internal test helper
        backend._rooms.setdefault(room, set()).add(ghost_id)


@pytest.mark.asyncio
async def test_send_to_connection_sends_message() -> None:
    """Send a message to a single connection."""
    mgr = WebSocketConnectionManager()
    ws = DummyWebSocket()
    await mgr.add_connection("a", ws)

    await mgr.send_to_connection("a", {"hello": "world"})

    assert ws.messages == [{"hello": "world"}]


@pytest.mark.asyncio
async def test_send_to_connection_propagates_error() -> None:
    """Errors raised by send_media bubble up."""
    mgr = WebSocketConnectionManager()
    ws = ErrorWebSocket()
    await mgr.add_connection("a", ws)

    with pytest.raises(RuntimeError):
        await mgr.send_to_connection("a", "ping")


@pytest.mark.asyncio
async def test_add_connection_raises_on_duplicate_id() -> None:
    """Adding a duplicate connection ID fails."""
    mgr = WebSocketConnectionManager()
    ws1 = DummyWebSocket()
    ws2 = DummyWebSocket()
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
)
async def test_broadcast_to_room_with_exclusion_scenarios(
    room_with_two_connections: tuple[
        WebSocketConnectionManager, DummyWebSocket, DummyWebSocket
    ],
    exclude: set[str] | None,
    expected_ws1: list[str],
    expected_ws2: list[str],
) -> None:
    """Test broadcasting to room with various exclusion scenarios."""
    mgr, ws1, ws2 = room_with_two_connections

    await mgr.broadcast_to_room("lobby", "hi", exclude=exclude)

    assert ws1.messages == expected_ws1
    assert ws2.messages == expected_ws2


@pytest.mark.asyncio
async def test_broadcast_to_room_propagates_error() -> None:
    """Broadcasting propagates errors from any connection."""
    mgr = WebSocketConnectionManager()
    ws1 = DummyWebSocket()
    ws2 = ErrorWebSocket()
    await mgr.add_connection("a", ws1)
    await mgr.add_connection("b", ws2)
    await mgr.join_room("a", "lobby")
    await mgr.join_room("b", "lobby")

    with pytest.raises(RuntimeError):
        await mgr.broadcast_to_room("lobby", 42)


@pytest.mark.asyncio
async def test_broadcast_to_room_aggregates_multiple_errors() -> None:
    """Aggregates exceptions when several sends fail."""
    mgr = WebSocketConnectionManager()
    ws1 = ErrorWebSocket()
    ws2 = ErrorWebSocket()
    await mgr.add_connection("a", ws1)
    await mgr.add_connection("b", ws2)
    await mgr.join_room("a", "lobby")
    await mgr.join_room("b", "lobby")

    eg = getattr(builtins, "ExceptionGroup", None)
    if eg is not None:
        with pytest.raises(eg) as excinfo:
            await mgr.broadcast_to_room("lobby", 42)
        assert len(getattr(excinfo.value, "exceptions", [])) == 2
    else:  # pragma: no cover - Python < 3.11
        with pytest.raises(RuntimeError):
            await mgr.broadcast_to_room("lobby", 42)


@pytest.mark.asyncio
async def test_join_room_requires_known_connection() -> None:
    """Joining a room with an unknown connection raises an error."""
    mgr = WebSocketConnectionManager()

    with pytest.raises(WebSocketConnectionNotFoundError):
        await mgr.join_room("ghost", "lobby")


@pytest.mark.asyncio
async def test_send_to_unknown_connection_raises_key_error() -> None:
    """Sending to an unknown connection raises
    WebSocketConnectionNotFoundError (a KeyError subclass).
    """
    mgr = WebSocketConnectionManager()

    with pytest.raises(WebSocketConnectionNotFoundError):
        await mgr.send_to_connection("a", "hi")


@pytest.mark.asyncio
async def test_connections_handle_room_filters(
    room_with_two_connections: tuple[
        WebSocketConnectionManager, DummyWebSocket, DummyWebSocket
    ],
) -> None:
    """Iterating yields all connections, room members, or nothing for empty rooms."""
    mgr, ws1, ws2 = room_with_two_connections

    assert {ws async for ws in mgr.connections()} == {ws1, ws2}
    assert {ws async for ws in mgr.connections(room="lobby")} == {ws1, ws2}
    assert [ws async for ws in mgr.connections(room="ghost")] == []


@pytest.mark.asyncio
async def test_connections_iterates_room_with_exclusion(
    room_with_two_connections: tuple[
        WebSocketConnectionManager, DummyWebSocket, DummyWebSocket
    ],
) -> None:
    """Iterating a room honours the exclusion list."""
    mgr, _, ws2 = room_with_two_connections

    seen = [ws async for ws in mgr.connections(room="lobby", exclude={"a"})]

    assert seen == [ws2]


@pytest.mark.asyncio
async def test_connections_ignore_unknown_ids_in_exclude(
    room_with_two_connections: tuple[
        WebSocketConnectionManager, DummyWebSocket, DummyWebSocket
    ],
) -> None:
    """Unknown IDs in ``exclude`` are ignored."""
    mgr, ws1, ws2 = room_with_two_connections

    seen = [ws async for ws in mgr.connections(room="lobby", exclude={"ghost"})]

    assert set(seen) == {ws1, ws2}


@pytest.mark.asyncio
async def test_connections_skip_stale_room_member(
    room_with_two_connections: tuple[
        WebSocketConnectionManager, DummyWebSocket, DummyWebSocket
    ],
) -> None:
    """Iterating a corrupted room skips ghost memberships."""
    mgr, ws1, ws2 = room_with_two_connections

    await corrupt_room_membership(mgr, "lobby", "ghost")

    seen = [ws async for ws in mgr.connections(room="lobby")]

    assert set(seen) == {ws1, ws2}


@pytest.mark.asyncio
async def test_broadcast_to_room_skips_stale_members(
    room_with_two_connections: tuple[
        WebSocketConnectionManager, DummyWebSocket, DummyWebSocket
    ],
) -> None:
    """Broadcasting ignores ghost memberships injected into the backend."""
    mgr, ws1, ws2 = room_with_two_connections

    await corrupt_room_membership(mgr, "lobby", "ghost")

    await mgr.broadcast_to_room("lobby", "hi")

    assert ws1.messages == ["hi"]
    assert ws2.messages == ["hi"]


@pytest.mark.asyncio
async def test_websockets_property_returns_snapshot() -> None:
    """Exposing websockets returns a stable snapshot."""
    mgr = WebSocketConnectionManager()
    ws = DummyWebSocket()
    await mgr.add_connection("a", ws)
    snapshot = mgr.websockets
    await mgr.add_connection("b", DummyWebSocket())
    assert dict(snapshot) == {"a": ws}


def test_default_backend_is_inprocess() -> None:
    """Ensure the default backend is used."""
    mgr = WebSocketConnectionManager()
    assert isinstance(mgr.backend, InProcessBackend)
    assert isinstance(mgr.backend, ConnectionBackend)


class RecordingBackend(ConnectionBackend):
    """Minimal custom backend used to verify delegation."""

    def __init__(self) -> None:
        self._websockets: dict[str, WebSocketLike] = {}
        self._rooms: dict[str, set[str]] = {}
        self.calls: list[str] = []

    @property
    def websockets(self) -> typ.Mapping[str, WebSocketLike]:
        """Expose a read-only snapshot of active websockets."""
        return types.MappingProxyType(self._websockets.copy())

    @property
    def rooms(self) -> typ.Mapping[str, typ.Collection[str]]:
        """Expose a read-only snapshot of room memberships."""
        snapshot = {room: set(ids) for room, ids in self._rooms.items()}
        return types.MappingProxyType(snapshot)

    async def add_connection(self, conn_id: str, ws: WebSocketLike) -> None:
        """Record a connection registration."""
        self.calls.append(f"add_connection:{conn_id}")
        if conn_id in self._websockets:
            msg = f"Duplicate connection ID: {conn_id!r}"
            raise ValueError(msg)
        self._websockets[conn_id] = ws

    async def remove_connection(self, conn_id: str) -> None:
        """Forget a connection and clean up empty rooms."""
        self.calls.append(f"remove_connection:{conn_id}")
        self._websockets.pop(conn_id, None)
        for members in self._rooms.values():
            members.discard(conn_id)
        self._rooms = {k: v for k, v in self._rooms.items() if v}

    async def join_room(self, conn_id: str, room: str) -> None:
        """Associate a connection with a room."""
        self.calls.append(f"join_room:{conn_id}:{room}")
        if conn_id not in self._websockets:
            raise WebSocketConnectionNotFoundError(conn_id)
        self._rooms.setdefault(room, set()).add(conn_id)

    async def leave_room(self, conn_id: str, room: str) -> None:
        """Remove a connection from a room if present."""
        self.calls.append(f"leave_room:{conn_id}:{room}")
        members = self._rooms.get(room)
        if members is None:
            return
        members.discard(conn_id)
        if not members:
            self._rooms.pop(room, None)

    async def get_websocket(self, conn_id: str) -> WebSocketLike | None:
        """Return the websocket for ``conn_id`` when known."""
        self.calls.append(f"get_websocket:{conn_id}")
        return self._websockets.get(conn_id)

    async def snapshot(
        self, room: str | None = None
    ) -> list[tuple[str, WebSocketLike]]:
        """Return a snapshot of members for the given room or all rooms."""
        label = room if room is not None else "*"
        self.calls.append(f"snapshot:{label}")
        if room is None:
            return list(self._websockets.items())
        member_ids = self._rooms.get(room, set())
        return [
            (cid, self._websockets[cid])
            for cid in member_ids
            if cid in self._websockets
        ]


@pytest.mark.asyncio
async def test_manager_uses_custom_backend() -> None:
    """Custom backends should drive storage and broadcasts."""
    backend = RecordingBackend()
    mgr = WebSocketConnectionManager(backend=backend)
    ws = DummyWebSocket()

    await mgr.add_connection("alice", ws)
    await mgr.join_room("alice", "crew")
    await mgr.broadcast_to_room("crew", {"msg": "hi"})
    await mgr.send_to_connection("alice", {"msg": "direct"})

    assert backend.calls == [
        "add_connection:alice",
        "join_room:alice:crew",
        "snapshot:crew",
        "get_websocket:alice",
    ]
    assert ws.messages == [{"msg": "hi"}, {"msg": "direct"}]
    assert backend.rooms == {"crew": {"alice"}}
