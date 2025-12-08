"""Behavioural test for pluggable connection manager backends."""

from __future__ import annotations

import dataclasses as dc
import types
import typing as typ

import pytest
from pytest_bdd import given, scenario, then, when

from falcon_pachinko.websocket import (
    ConnectionBackend,
    WebSocketConnectionManager,
    WebSocketConnectionNotFoundError,
)

if typ.TYPE_CHECKING:
    import asyncio

    from falcon_pachinko.protocols import WebSocketLike


@pytest.fixture
def event_loop(
    event_loop_policy: asyncio.AbstractEventLoopPolicy,
) -> typ.Iterator[asyncio.AbstractEventLoop]:
    """Provide a dedicated event loop for the scenario."""
    loop = event_loop_policy.new_event_loop()
    try:
        yield loop
    finally:
        loop.close()


class DummyWebSocket:
    """Minimal websocket stub that records sent messages."""

    def __init__(self) -> None:
        self.messages: list[object] = []

    async def accept(self, subprotocol: str | None = None) -> None:  # pragma: no cover
        """Accept the websocket connection (unused in this scenario)."""
        return

    async def close(self, code: int = 1000) -> None:  # pragma: no cover
        """Close the websocket connection (unused in this scenario)."""
        return

    async def send_media(self, data: object) -> None:
        """Record outbound messages."""
        self.messages.append(data)

    async def receive_media(self) -> object:  # pragma: no cover
        """Provide a placeholder receive implementation."""
        return None


class RecordingBackend(ConnectionBackend):
    """Custom backend that records calls for assertion."""

    def __init__(self) -> None:
        self._websockets: dict[str, WebSocketLike] = {}
        self._rooms: dict[str, set[str]] = {}
        self.calls: list[str] = []

    @property
    def websockets(self) -> typ.Mapping[str, WebSocketLike]:
        """Expose a read-only snapshot of active websockets."""
        return types.MappingProxyType(self._websockets)

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
        """Forget a connection and purge empty rooms."""
        self.calls.append(f"remove_connection:{conn_id}")
        self._websockets.pop(conn_id, None)
        for members in self._rooms.values():
            members.discard(conn_id)
        self._rooms = {room: ids for room, ids in self._rooms.items() if ids}

    async def join_room(self, conn_id: str, room: str) -> None:
        """Associate a connection with a room."""
        self.calls.append(f"join_room:{conn_id}:{room}")
        if conn_id not in self._websockets:
            raise WebSocketConnectionNotFoundError(conn_id)
        self._rooms.setdefault(room, set()).add(conn_id)

    async def leave_room(self, conn_id: str, room: str) -> None:
        """Remove a connection from ``room`` if present."""
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
        """Return a snapshot of members for a room or all connections."""
        label = room if room is not None else "*"
        self.calls.append(f"snapshot:{label}")
        if room is None:
            return list(self._websockets.items())
        members = self._rooms.get(room, set())
        return [
            (cid, self._websockets[cid]) for cid in members if cid in self._websockets
        ]


@dc.dataclass
class ScenarioState:
    """Share state across steps."""

    manager: WebSocketConnectionManager
    backend: RecordingBackend
    websocket: DummyWebSocket
    event_loop: asyncio.AbstractEventLoop


@scenario(
    "custom_connection_backend.feature",
    "broadcast through a custom backend",
)
def test_custom_backend_broadcast() -> None:  # pragma: no cover - BDD registration
    """Scenario registration for pytest-bdd."""


@given(
    "a connection manager configured with a recording backend",
    target_fixture="context",
)
def given_manager(event_loop: asyncio.AbstractEventLoop) -> ScenarioState:
    """Create a manager that uses the recording backend."""
    backend = RecordingBackend()
    manager = WebSocketConnectionManager(backend=backend)
    websocket = DummyWebSocket()
    event_loop.run_until_complete(manager.add_connection("alice", websocket))
    event_loop.run_until_complete(manager.join_room("alice", "crew"))
    return ScenarioState(
        manager=manager, backend=backend, websocket=websocket, event_loop=event_loop
    )


@when(
    'a message is broadcast to room "crew" via the manager',
    target_fixture="context",
)
def when_broadcast(context: ScenarioState) -> ScenarioState:
    """Broadcast a payload through the connection manager."""
    context.event_loop.run_until_complete(
        context.manager.broadcast_to_room("crew", {"msg": "hello"})
    )
    return context


@then("the backend records the broadcast snapshot")
def then_backend_calls(context: ScenarioState) -> None:
    """Ensure the backend snapshot call is recorded."""
    assert "snapshot:crew" in context.backend.calls
    assert context.backend.rooms == {"crew": {"alice"}}


@then("the websocket receives the broadcast payload")
def then_websocket_receives(context: ScenarioState) -> None:
    """Verify the websocket saw the payload."""
    assert context.websocket.messages == [{"msg": "hello"}]
