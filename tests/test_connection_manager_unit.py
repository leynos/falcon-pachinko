"""Unit tests for the WebSocketConnectionManager."""

from __future__ import annotations

import pytest
import pytest_asyncio

from falcon_pachinko.websocket import (
    WebSocketConnectionManager,
    WebSocketConnectionNotFoundError,
)


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
async def test_join_room_requires_known_connection() -> None:
    """Joining a room with an unknown connection raises an error."""
    mgr = WebSocketConnectionManager()

    with pytest.raises(WebSocketConnectionNotFoundError):
        await mgr.join_room("ghost", "lobby")


@pytest.mark.asyncio
async def test_send_to_unknown_connection_raises_key_error() -> None:
    """Sending to an unknown connection raises KeyError."""
    mgr = WebSocketConnectionManager()

    with pytest.raises(WebSocketConnectionNotFoundError):
        await mgr.send_to_connection("a", "hi")


@pytest.mark.asyncio
async def test_connections_iterates_all(
    room_with_two_connections: tuple[
        WebSocketConnectionManager, DummyWebSocket, DummyWebSocket
    ],
) -> None:
    """Iterating without a room yields all connections."""
    mgr, ws1, ws2 = room_with_two_connections

    seen = [ws async for ws in mgr.connections()]

    assert set(seen) == {ws1, ws2}


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
async def test_get_filtered_connection_ids_handles_rooms(
    room_with_two_connections: tuple[
        WebSocketConnectionManager, DummyWebSocket, DummyWebSocket
    ],
) -> None:
    """The ID filter returns all, room members, or empty list."""
    mgr, _, _ = room_with_two_connections

    assert set(mgr._get_filtered_connection_ids(None)) == {"a", "b"}
    assert set(mgr._get_filtered_connection_ids("lobby")) == {"a", "b"}
    assert mgr._get_filtered_connection_ids("ghost") == []


@pytest.mark.asyncio
async def test_should_include_connection_validates_and_filters(
    room_with_two_connections: tuple[
        WebSocketConnectionManager, DummyWebSocket, DummyWebSocket
    ],
) -> None:
    """Helper validates existence and respects exclusions."""
    mgr, _, _ = room_with_two_connections

    assert mgr._should_include_connection("a", None) is True
    assert mgr._should_include_connection("a", {"a"}) is False
    with pytest.raises(WebSocketConnectionNotFoundError):
        mgr._should_include_connection("ghost", None)


@pytest.mark.asyncio
async def test_build_websocket_list_filters_and_validates(
    room_with_two_connections: tuple[
        WebSocketConnectionManager, DummyWebSocket, DummyWebSocket
    ],
) -> None:
    """Websocket list omits excluded IDs and raises for missing ones."""
    mgr, ws1, ws2 = room_with_two_connections

    websockets = mgr._build_websocket_list(["a", "b"], {"b"})
    assert websockets == [ws1]

    with pytest.raises(WebSocketConnectionNotFoundError):
        mgr._build_websocket_list(["a", "ghost"], None)
