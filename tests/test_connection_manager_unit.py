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
async def test_broadcast_to_room_sends_to_each_member(
    room_with_two_connections: tuple[
        WebSocketConnectionManager, DummyWebSocket, DummyWebSocket
    ],
) -> None:
    """Broadcast to a room sends to all connections."""
    mgr, ws1, ws2 = room_with_two_connections

    await mgr.broadcast_to_room("lobby", "hi")

    assert ws1.messages == ["hi"]
    assert ws2.messages == ["hi"]


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
async def test_broadcast_to_room_excludes_connections(
    room_with_two_connections: tuple[
        WebSocketConnectionManager, DummyWebSocket, DummyWebSocket
    ],
) -> None:
    """Excluded connections do not receive the message."""
    mgr, ws1, ws2 = room_with_two_connections

    await mgr.broadcast_to_room("lobby", "hi", exclude={"a"})

    assert ws1.messages == []
    assert ws2.messages == ["hi"]


@pytest.mark.asyncio
async def test_send_to_unknown_connection_raises_key_error() -> None:
    """Sending to an unknown connection raises KeyError."""
    mgr = WebSocketConnectionManager()

    with pytest.raises(WebSocketConnectionNotFoundError):
        await mgr.send_to_connection("a", "hi")
