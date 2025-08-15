"""Behaviour tests for the WebSocketConnectionManager."""

from __future__ import annotations

import asyncio
import dataclasses as dc

from pytest_bdd import given, scenario, then, when

from falcon_pachinko.websocket import WebSocketConnectionManager


@dc.dataclass
class DummyWebSocket:
    """WebSocket stub that records messages."""

    messages: list[object]

    async def accept(
        self, subprotocol: str | None = None
    ) -> None:  # pragma: no cover - unused
        """Accept the connection."""
        return

    async def close(self, code: int = 1000) -> None:  # pragma: no cover - unused
        """Close the connection."""
        return

    async def send_media(self, data: object) -> None:
        """Record sent data."""
        self.messages.append(data)


@scenario(
    "connection_manager.feature", "broadcast message to all connections in a room"
)
def test_broadcast() -> None:  # pragma: no cover - bdd registration
    """Scenario: broadcast message to all connections in a room."""


@given(
    'a connection manager with two connections in room "lobby"',
    target_fixture="setup",
)
def setup_room() -> tuple[WebSocketConnectionManager, DummyWebSocket, DummyWebSocket]:
    """Create a connection manager prepopulated with a lobby room."""
    mgr = WebSocketConnectionManager()
    ws1 = DummyWebSocket(messages=[])
    ws2 = DummyWebSocket(messages=[])
    mgr.add_connection("a", ws1)
    mgr.add_connection("b", ws2)
    mgr.join_room("a", "lobby")
    mgr.join_room("b", "lobby")
    return mgr, ws1, ws2


@when('a message is broadcast to room "lobby"')
def broadcast(
    setup: tuple[WebSocketConnectionManager, DummyWebSocket, DummyWebSocket],
) -> None:
    """Broadcast a test message to the lobby room."""
    mgr, _, _ = setup

    async def _run() -> None:
        await mgr.broadcast_to_room("lobby", {"msg": "hi"})

    asyncio.run(_run())


@then("both connections receive that message")
def assert_received(
    setup: tuple[WebSocketConnectionManager, DummyWebSocket, DummyWebSocket],
) -> None:
    """Assert that both connections received the broadcast."""
    _, ws1, ws2 = setup
    assert ws1.messages == [{"msg": "hi"}]
    assert ws2.messages == [{"msg": "hi"}]
