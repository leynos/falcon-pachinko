"""Behavioural tests covering the full reference example workflow."""

from __future__ import annotations

import asyncio
import dataclasses as dc
import typing as typ

import msgspec.json as msjson
import pytest
from pytest_bdd import given, scenario, then, when

from examples.reference_app import build_container, build_router
from examples.reference_app.resources import AddTask, TaskStreamResource
from examples.reference_app.services import AnnouncementFeed
from falcon_pachinko.testing import WebSocketSimulator
from falcon_pachinko.websocket import WebSocketConnectionManager
from tests._stubs import RequestStub

if typ.TYPE_CHECKING:  # pragma: no cover - typing helpers, string-only annotations
    import collections.abc as cabc

    from falcon_pachinko import ServiceContainer, WebSocketResource, WebSocketRouter


@dc.dataclass(slots=True)
class ReferenceScenario:
    """Container for shared reference example state."""

    router: WebSocketRouter
    container: ServiceContainer
    feed: AnnouncementFeed
    simulator: WebSocketSimulator
    instances: list[WebSocketResource]
    resource: TaskStreamResource | None = None
    last_event: tuple[str, dict[str, object]] | None = None


_MISSING_TASK_RESOURCE_MSG = "TaskStreamResource was not instantiated"


@pytest.fixture
def event_loop() -> cabc.Iterator[asyncio.AbstractEventLoop]:
    """Provide an isolated event loop per test."""
    loop = asyncio.new_event_loop()
    try:
        yield loop
    finally:
        loop.close()


@scenario(
    "reference_example.feature",
    "Task creation flows through the router, schema dispatch, and feed",
)
def test_reference_example() -> None:  # pragma: no cover - scenario registration
    """Scenario registration for pytest-bdd."""


@given("the reference router with a recording factory", target_fixture="context")
def given_reference_router(event_loop: asyncio.AbstractEventLoop) -> ReferenceScenario:
    """Build the router wiring using the shared DI container."""
    conn_mgr = WebSocketConnectionManager()
    container = build_container(conn_mgr)
    feed = container.resolve("announcement_feed")
    assert isinstance(feed, AnnouncementFeed), (
        "the container must provide the announcement feed service"
    )
    instances: list[WebSocketResource] = []

    def recording_factory(
        route_factory: cabc.Callable[..., WebSocketResource],
    ) -> WebSocketResource:
        instance = container.create_resource(route_factory)
        instances.append(instance)
        return instance

    simulator = WebSocketSimulator()
    router = build_router(
        container,
        simulator_factory=lambda *_: simulator,
        resource_factory=recording_factory,
    )
    return ReferenceScenario(
        router=router,
        container=container,
        feed=feed,
        simulator=simulator,
        instances=instances,
    )


def _select_task_resource(instances: list[WebSocketResource]) -> TaskStreamResource:
    for instance in reversed(instances):
        if isinstance(instance, TaskStreamResource):
            return instance
    raise AssertionError(_MISSING_TASK_RESOURCE_MSG)


@when(
    'a client connects to "/ws/workspaces/atlas/projects/triage/tasks" '
    'using token "seekrit" as user "casey"',
    target_fixture="context",
)
def when_client_connects(
    context: ReferenceScenario, event_loop: asyncio.AbstractEventLoop
) -> ReferenceScenario:
    """Dispatch a connection through the router with valid headers."""
    req = RequestStub(
        "/ws/workspaces/atlas/projects/triage/tasks",
        headers={"x-workspace-token": "seekrit", "x-user": "casey"},
    )
    event_loop.run_until_complete(context.router.on_websocket(req, context.simulator))
    context.resource = _select_task_resource(context.instances)
    return context


@when('they send a "task.add" message for task "T-42"', target_fixture="context")
def when_send_task_add(
    context: ReferenceScenario, event_loop: asyncio.AbstractEventLoop
) -> ReferenceScenario:
    """Dispatch a schema-defined message through the active resource."""
    resource = context.resource
    assert resource is not None, "the connection step must have selected the resource"
    payload = AddTask(task_id="T-42", title="Investigate event loop")
    raw = msjson.encode(payload)
    event_loop.run_until_complete(resource.dispatch(context.simulator, raw))
    context.last_event = event_loop.run_until_complete(context.feed.next_event())
    return context


@then("the connection is accepted")
def then_connection(context: ReferenceScenario) -> None:
    """Ensure the simulator recorded the handshake acceptance."""
    assert context.simulator.accepted is True, "the simulator must record acceptance"


@then("the task stream resource replies with a task acknowledgement")
def then_acknowledgement(context: ReferenceScenario) -> None:
    """Check that the last frame is the expected acknowledgement."""
    message = context.simulator.sent_messages[-1]
    assert isinstance(message, dict), "the last frame must be a mapping"
    assert message["type"] == "task.added", (
        "the last frame must be a task.added acknowledgement"
    )


@then('the announcement feed captures an event for workspace "atlas"')
def then_feed_capture(context: ReferenceScenario) -> None:
    """Validate that the AnnouncementFeed observed the broadcast event."""
    assert context.last_event is not None, "the feed must have captured an event"
    workspace, payload = context.last_event
    assert workspace == "atlas", "the event must be scoped to workspace 'atlas'"
    nested = payload["payload"]
    assert isinstance(nested, dict), "the broadcast event must nest a payload mapping"
    assert nested["kind"] == "task_added", (
        "the nested payload must be a task_added event"
    )
