"""Behavioural tests covering the full reference example workflow."""

from __future__ import annotations

import asyncio
import dataclasses as dc
import typing as typ

import pytest
from pytest_bdd import given, scenario, then, when

from examples.reference_app import build_container
from examples.reference_app.resources import (
    AddTask,
    TaskStreamResource,
    WorkspaceResource,
    register_reference_hooks,
)
from examples.reference_app.server import _require_token_hook
from examples.reference_app.services import (
    AnnouncementFeed,
    AuditTrail,
    TokenAuthenticator,
)
from falcon_pachinko.hooks import HookEvent
from falcon_pachinko.testing import SimulatorConnection, SimulatorRouterHarness
from falcon_pachinko.websocket import WebSocketConnectionManager

if typ.TYPE_CHECKING:  # pragma: no cover - typing helpers, string-only annotations
    import collections.abc as cabc

    from falcon_pachinko import ServiceContainer, WebSocketResource


@dc.dataclass(slots=True)
class ReferenceScenario:
    """Container for shared reference example state."""

    harness: SimulatorRouterHarness
    container: ServiceContainer
    feed: AnnouncementFeed
    instances: list[WebSocketResource]
    connection: SimulatorConnection | None = None
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

    register_reference_hooks()
    harness = SimulatorRouterHarness(
        mount="/ws",
        resource_factory=recording_factory,
    )
    harness.router.add_route("/workspaces/{workspace_id}", WorkspaceResource)
    harness.router.global_hooks.add(
        HookEvent.BEFORE_CONNECT,
        _require_token_hook(
            typ.cast("TokenAuthenticator", container.resolve("token_authenticator")),
            typ.cast("AuditTrail", container.resolve("audit_trail")),
        ),
    )
    return ReferenceScenario(
        harness=harness,
        container=container,
        feed=feed,
        instances=instances,
    )


def _select_task_resource(instances: list[WebSocketResource]) -> TaskStreamResource:
    for instance in reversed(instances):
        if isinstance(instance, TaskStreamResource):
            return instance
    raise AssertionError(_MISSING_TASK_RESOURCE_MSG)


@when(
    'a client connects to "/ws/workspaces/atlas/projects/triage/tasks" '
    'using token "seekrit" as user "casey" and sends a "task.add" message '
    'for task "T-42"',
    target_fixture="context",
)
def when_client_connects(
    context: ReferenceScenario, event_loop: asyncio.AbstractEventLoop
) -> ReferenceScenario:
    """Connect through the harness and send a task-add frame."""
    payload = AddTask(task_id="T-42", title="Investigate event loop")

    async def exchange() -> None:
        async with context.harness.connect(
            "/workspaces/atlas/projects/triage/tasks",
            headers={"x-workspace-token": "seekrit", "x-user": "casey"},
            initial_inbound=[(payload, "json")],
        ) as connection:
            context.connection = connection
            context.resource = _select_task_resource(context.instances)
            context.last_event = await context.feed.next_event()

    event_loop.run_until_complete(exchange())
    return context


@then("the connection is accepted")
def then_connection(context: ReferenceScenario) -> None:
    """Ensure the simulator recorded the handshake acceptance."""
    connection = context.connection
    assert connection is not None, "the harness must yield a connection"
    assert connection.accepted is True, "the simulator must record acceptance"


@then("the task stream resource replies with a task acknowledgement")
def then_acknowledgement(context: ReferenceScenario) -> None:
    """Check that the last frame is the expected acknowledgement."""
    connection = context.connection
    assert connection is not None, "the harness must yield a connection"
    message = connection.sent_messages[-1]
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
