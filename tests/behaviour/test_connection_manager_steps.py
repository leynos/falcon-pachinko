"""Behaviour tests for the WebSocketConnectionManager."""

from __future__ import annotations

import asyncio
import typing as typ

from pytest_bdd import given, scenario, then, when

from falcon_pachinko.websocket import WebSocketConnectionManager
from tests._stubs import RecordingWebSocket

if typ.TYPE_CHECKING:
    from falcon_pachinko.protocols import WebSocketLike

type SetupFixture = tuple[
    WebSocketConnectionManager,
    RecordingWebSocket,
    RecordingWebSocket,
    asyncio.AbstractEventLoop,
]


def _broadcast_to_lobby(setup: SetupFixture, exclude: set[str] | None = None) -> None:
    """Broadcast a test message to the lobby."""
    mgr, _, _, loop = setup
    loop.run_until_complete(
        mgr.broadcast_to_room("lobby", {"msg": "hi"}, exclude=exclude)
    )


def _assert_messages_received(
    setup: SetupFixture,
    expected_ws1: list[object],
    expected_ws2: list[object],
) -> None:
    """Assert that each websocket received the expected messages."""
    _, ws1, ws2, loop = setup
    try:
        assert ws1.messages == expected_ws1, (
            f"ws1 should have received {expected_ws1!r}, got {ws1.messages!r}"
        )
        assert ws2.messages == expected_ws2, (
            f"ws2 should have received {expected_ws2!r}, got {ws2.messages!r}"
        )
    finally:
        loop.close()


async def _iterate_lobby(
    mgr: WebSocketConnectionManager,
    exclude: set[str] | None = None,
) -> list[WebSocketLike]:
    """Collect websockets yielded when iterating the lobby."""
    return [ws async for ws in mgr.connections(room="lobby", exclude=exclude)]


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


@scenario(
    "connection_manager.feature",
    "iterate over connections in a room",
)
def test_iterate_lobby() -> None:  # pragma: no cover - bdd registration
    """Scenario: iterate over connections in a room."""


@scenario(
    "connection_manager.feature",
    "iterate over connections in a room with one connection excluded",
)
def test_iterate_lobby_excluding() -> None:  # pragma: no cover - bdd registration
    """Scenario: iterate over connections in a room with one connection excluded."""


@scenario(
    "connection_manager.feature",
    "iterate over an empty room",
)
def test_iterate_empty_room() -> None:  # pragma: no cover - bdd registration
    """Scenario: iterate over an empty room."""


@given(
    'a connection manager with two connections in room "lobby"',
    target_fixture="setup",
)
def setup_room() -> SetupFixture:
    """Create a connection manager prepopulated with a lobby room."""
    loop = asyncio.new_event_loop()
    try:
        asyncio.set_event_loop(loop)
        mgr = WebSocketConnectionManager()
        ws1 = RecordingWebSocket()
        ws2 = RecordingWebSocket()
        loop.run_until_complete(mgr.add_connection("a", ws1))
        loop.run_until_complete(mgr.add_connection("b", ws2))
        loop.run_until_complete(mgr.join_room("a", "lobby"))
        loop.run_until_complete(mgr.join_room("b", "lobby"))
        return mgr, ws1, ws2, loop
    finally:
        asyncio.set_event_loop(None)


@given(
    'a connection manager with no connections in room "lobby"',
    target_fixture="setup",
)
def setup_empty_room() -> SetupFixture:
    """Create a connection manager with an empty lobby room.

    Note: ``ws1``/``ws2`` are placeholders to satisfy ``SetupFixture`` shape.

    Returns
    -------
    SetupFixture
        The manager, placeholder websockets, and the event loop.
    """
    loop = asyncio.new_event_loop()
    try:
        asyncio.set_event_loop(loop)
        mgr = WebSocketConnectionManager()
        ws1 = RecordingWebSocket()
        ws2 = RecordingWebSocket()
        return mgr, ws1, ws2, loop
    finally:
        asyncio.set_event_loop(None)


@when('a message is broadcast to room "lobby"')
def broadcast(setup: SetupFixture) -> None:
    """Broadcast a test message to the lobby room."""
    _broadcast_to_lobby(setup)


@when('a message is broadcast to room "lobby" excluding connection "a"')
def broadcast_excluding(setup: SetupFixture) -> None:
    """Broadcast a test message excluding connection ``a``."""
    _broadcast_to_lobby(setup, exclude={"a"})


@when('we iterate over connections in room "lobby"', target_fixture="iterated")
def iterate_lobby(setup: SetupFixture) -> list[WebSocketLike]:
    """Collect websockets by iterating the lobby."""
    mgr, _, _, loop = setup
    return loop.run_until_complete(_iterate_lobby(mgr))


@when(
    'we iterate over connections in room "lobby" excluding connection "a"',
    target_fixture="iterated",
)
def iterate_lobby_excluding(setup: SetupFixture) -> list[WebSocketLike]:
    """Collect websockets by iterating the lobby without connection ``a``."""
    mgr, _, _, loop = setup
    return loop.run_until_complete(_iterate_lobby(mgr, exclude={"a"}))


@then("both connections receive that message")
def assert_received(setup: SetupFixture) -> None:
    """Assert that both connections received the broadcast."""
    _assert_messages_received(setup, [{"msg": "hi"}], [{"msg": "hi"}])


@then('only connection "b" receives that message')
def assert_received_excluding(setup: SetupFixture) -> None:
    """Assert that only connection ``b`` receives the broadcast."""
    _assert_messages_received(setup, [], [{"msg": "hi"}])


@then("both connections are yielded")
def assert_iterated(setup: SetupFixture, iterated: list[WebSocketLike]) -> None:
    """Assert that iteration returned both websockets."""
    _, ws1, ws2, loop = setup
    try:
        ids = {id(ws) for ws in iterated}
        assert ids == {id(ws1), id(ws2)}, "both connections should be yielded"
    finally:
        loop.close()


@then("only the non-excluded connection is yielded")
def assert_iterated_excluding(
    setup: SetupFixture, iterated: list[WebSocketLike]
) -> None:
    """Assert that iteration excludes the specified websocket."""
    _, ws1, ws2, loop = setup
    try:
        ids = {id(ws) for ws in iterated}
        assert ids == {id(ws2)}, "only the non-excluded connection should be yielded"
        assert id(ws1) not in ids, "the excluded connection must not be yielded"
    finally:
        loop.close()


@then("no connections are yielded for an empty room")
def assert_iterated_empty_room(
    setup: SetupFixture, iterated: list[WebSocketLike]
) -> None:
    """Assert that iteration over an empty room yields nothing."""
    _, _, _, loop = setup
    try:
        assert iterated == [], "an empty room must yield no connections"
    finally:
        loop.close()
