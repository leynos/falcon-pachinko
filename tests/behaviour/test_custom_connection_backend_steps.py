"""Behavioural test for pluggable connection manager backends."""

from __future__ import annotations

import dataclasses as dc
import typing as typ

import pytest
from pytest_bdd import given, scenario, then, when

from falcon_pachinko.websocket import WebSocketConnectionManager
from tests._stubs import RecordingBackend, RecordingWebSocket

if typ.TYPE_CHECKING:
    import asyncio
    import collections.abc as cabc


@pytest.fixture
def event_loop(
    event_loop_policy: asyncio.AbstractEventLoopPolicy,
) -> cabc.Iterator[asyncio.AbstractEventLoop]:
    """Provide a dedicated event loop for the scenario."""
    loop = event_loop_policy.new_event_loop()
    try:
        yield loop
    finally:
        loop.close()


@dc.dataclass(slots=True)
class ScenarioState:
    """Share state across steps."""

    manager: WebSocketConnectionManager
    backend: RecordingBackend
    websocket: RecordingWebSocket
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
    websocket = RecordingWebSocket()
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
    assert "snapshot:crew" in context.backend.calls, (
        "broadcasting must record a snapshot call for the room"
    )
    assert context.backend.rooms == {"crew": {"alice"}}, (
        "the crew room must still contain only alice"
    )


@then("the websocket receives the broadcast payload")
def then_websocket_receives(context: ScenarioState) -> None:
    """Verify the websocket saw the payload."""
    assert context.websocket.messages == [{"msg": "hello"}], (
        "the websocket should have received the broadcast payload"
    )
