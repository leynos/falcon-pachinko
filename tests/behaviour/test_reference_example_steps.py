"""Behavioural tests covering the full reference example workflow."""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses as dc
import typing as typ

import pytest
from pytest_bdd import given, scenario, then, when

from examples.reference_app import build_container
from examples.reference_app.resources import (
    AddTask,
    TaskStreamResource,
    WorkspaceResource,
    _workspace_room,
    register_reference_hooks,
)
from examples.reference_app.server import _require_token_hook
from examples.reference_app.services import (
    AnnouncementFeed,
    AuditTrail,
    TokenAuthenticator,
)
from examples.reference_app.workers import announcement_worker
from falcon_pachinko.hooks import HookEvent
from falcon_pachinko.testing import SimulatorConnection, SimulatorRouterHarness
from falcon_pachinko.websocket import WebSocketConnectionManager
from falcon_pachinko.workers import WorkerController
from tests._stubs import DisconnectingWebSocket, SignallingWebSocket

if typ.TYPE_CHECKING:  # pragma: no cover - typing helpers, string-only annotations
    import collections.abc as cabc

    from falcon_pachinko import (
        ServiceContainer,
        WebSocketLike,
        WebSocketResource,
    )


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
        """Create the route resource through the container and keep it."""
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
    """Return the most recent TaskStreamResource, or fail if none exists."""
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
        """Wait for the routed task's announcement within a bounded session."""
        async with context.harness.connect(
            "/workspaces/atlas/projects/triage/tasks",
            headers={"x-workspace-token": "seekrit", "x-user": "casey"},
            initial_inbound=[(payload, "json")],
        ) as connection:
            context.connection = connection
            context.resource = _select_task_resource(context.instances)
            async with asyncio.timeout(5):
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


ANNOUNCEMENT_TIMEOUT = 5.0


@dc.dataclass(slots=True)
class AnnouncementScenario:
    """State for the announcement worker's recipient-failure scenario."""

    loop: asyncio.AbstractEventLoop
    controller: WorkerController
    feed: AnnouncementFeed
    disconnecting: DisconnectingWebSocket
    atlas_healthy: SignallingWebSocket
    zephyr_healthy: SignallingWebSocket
    atlas_received: asyncio.Event
    zephyr_received: asyncio.Event


@scenario(
    "reference_example.feature",
    "A disconnected recipient does not stop announcements for other workspaces",
)
def test_disconnected_recipient_keeps_announcements_flowing() -> None:
    """Scenario registration for pytest-bdd."""


@pytest.fixture
def announcement_scenario(
    event_loop: asyncio.AbstractEventLoop,
) -> cabc.Iterator[AnnouncementScenario]:
    """Run the real announcement worker over the container's feed and manager."""
    conn_mgr = WebSocketConnectionManager()
    container = build_container(conn_mgr)
    feed = container.resolve("announcement_feed")
    assert isinstance(feed, AnnouncementFeed), (
        "the container must provide the announcement feed service"
    )
    atlas_received = asyncio.Event()
    zephyr_received = asyncio.Event()
    disconnecting = DisconnectingWebSocket()
    atlas_healthy = SignallingWebSocket(expected=2, received=atlas_received)
    zephyr_healthy = SignallingWebSocket(expected=1, received=zephyr_received)

    async def _register(
        conn_id: str, websocket: WebSocketLike, workspace_id: str
    ) -> None:
        """Join ``conn_id`` to the room announcements for ``workspace_id`` use."""
        await conn_mgr.add_connection(conn_id, websocket)
        await conn_mgr.join_room(conn_id, _workspace_room(workspace_id))

    controller = WorkerController()
    event_loop.run_until_complete(
        _register("atlas-gone", disconnecting, "atlas"),
    )
    event_loop.run_until_complete(_register("atlas-healthy", atlas_healthy, "atlas"))
    event_loop.run_until_complete(_register("zephyr-healthy", zephyr_healthy, "zephyr"))
    event_loop.run_until_complete(
        controller.start(
            announcement_worker,
            conn_mgr=conn_mgr,
            announcement_feed=feed,
        )
    )
    try:
        yield AnnouncementScenario(
            loop=event_loop,
            controller=controller,
            feed=feed,
            disconnecting=disconnecting,
            atlas_healthy=atlas_healthy,
            zephyr_healthy=zephyr_healthy,
            atlas_received=atlas_received,
            zephyr_received=zephyr_received,
        )
    finally:
        with contextlib.suppress(Exception):
            event_loop.run_until_complete(controller.stop())


@given(
    "an announcement worker with a disconnected atlas recipient",
    target_fixture="announcement_scenario",
)
def given_announcement_worker(
    announcement_scenario: AnnouncementScenario,
) -> AnnouncementScenario:
    """Reuse the running worker started by the scenario fixture."""
    return announcement_scenario


@when("announcements are published for the atlas and zephyr workspaces")
def publish_announcements(announcement_scenario: AnnouncementScenario) -> None:
    """Publish to both workspaces and wait for the healthy recipients."""

    async def _publish() -> None:
        """Publish events and wait for the healthy recipients to receive them."""
        await announcement_scenario.feed.publish("atlas", {"seq": 1})
        await announcement_scenario.feed.publish("atlas", {"seq": 2})
        await announcement_scenario.feed.publish("zephyr", {"seq": 3})
        async with asyncio.timeout(ANNOUNCEMENT_TIMEOUT):
            await announcement_scenario.atlas_received.wait()
            await announcement_scenario.zephyr_received.wait()

    announcement_scenario.loop.run_until_complete(_publish())


@then("healthy recipients in both workspaces receive every announcement")
def then_healthy_recipients_receive(
    announcement_scenario: AnnouncementScenario,
) -> None:
    """Check that one disconnect did not interrupt cross-workspace delivery."""
    assert announcement_scenario.atlas_healthy.messages == [{"seq": 1}, {"seq": 2}], (
        "the healthy atlas recipient must receive both announcements in order"
    )
    assert announcement_scenario.zephyr_healthy.messages == [{"seq": 3}], (
        "the zephyr recipient must still receive its announcement"
    )
    assert announcement_scenario.disconnecting.messages == [], (
        "the disconnected recipient must not record a delivery"
    )
