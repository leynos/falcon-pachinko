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


@scenario(
    "connection_manager.feature",
    "broadcast message to a room with one connection excluded",
)
def test_broadcast_with_exclusion() -> None:  # pragma: no cover - bdd registration
    """Scenario: broadcast message to a room with one connection excluded."""


@given(
    'a connection manager with two connections in room "lobby"',
    target_fixture="setup",
)
def setup_room() -> tuple[
    WebSocketConnectionManager,
    DummyWebSocket,
    DummyWebSocket,
    asyncio.AbstractEventLoop,
]:
    """Create a connection manager prepopulated with a lobby room."""
    loop = asyncio.new_event_loop()
    try:
        asyncio.set_event_loop(loop)
        mgr = WebSocketConnectionManager()
        ws1 = DummyWebSocket(messages=[])
        ws2 = DummyWebSocket(messages=[])
        loop.run_until_complete(mgr.add_connection("a", ws1))
        loop.run_until_complete(mgr.add_connection("b", ws2))
        loop.run_until_complete(mgr.join_room("a", "lobby"))
        loop.run_until_complete(mgr.join_room("b", "lobby"))
        return mgr, ws1, ws2, loop
    finally:
        asyncio.set_event_loop(None)


@when('a message is broadcast to room "lobby"')
def broadcast(
    setup: tuple[
        WebSocketConnectionManager,
        DummyWebSocket,
        DummyWebSocket,
        asyncio.AbstractEventLoop,
    ],
) -> None:
    """Broadcast a test message to the lobby room."""
    mgr, _, _, loop = setup
    loop.run_until_complete(mgr.broadcast_to_room("lobby", {"msg": "hi"}))


@when('a message is broadcast to room "lobby" excluding connection "a"')
def broadcast_excluding(
    setup: tuple[
        WebSocketConnectionManager,
        DummyWebSocket,
        DummyWebSocket,
        asyncio.AbstractEventLoop,
    ],
) -> None:
    """Broadcast a test message excluding connection ``a``."""
    mgr, _, _, loop = setup
    loop.run_until_complete(
        mgr.broadcast_to_room("lobby", {"msg": "hi"}, exclude={"a"})
    )


@then("both connections receive that message")
def assert_received(
    setup: tuple[
        WebSocketConnectionManager,
        DummyWebSocket,
        DummyWebSocket,
        asyncio.AbstractEventLoop,
    ],
) -> None:
    """Assert that both connections received the broadcast."""
    _, ws1, ws2, loop = setup
    try:
        assert ws1.messages == [{"msg": "hi"}]
        assert ws2.messages == [{"msg": "hi"}]
    finally:
        loop.close()


@then('only connection "b" receives that message')
def assert_received_excluding(
    setup: tuple[
        WebSocketConnectionManager,
        DummyWebSocket,
        DummyWebSocket,
        asyncio.AbstractEventLoop,
    ],
) -> None:
    """Assert that only connection ``b`` receives the broadcast."""
    _, ws1, ws2, loop = setup
    try:
        assert ws1.messages == []
        assert ws2.messages == [{"msg": "hi"}]
    finally:
        loop.close()
