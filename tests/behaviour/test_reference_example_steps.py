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
from tests._stubs import DisconnectingWebSocket

if typ.TYPE_CHECKING:  # pragma: no cover - typing helpers, string-only annotations
    import collections.abc as cabc

    from falcon_pachinko import (
        ServiceContainer,
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


def _build_reference_harness(
    instances: list[WebSocketResource] | None = None,
) -> tuple[SimulatorRouterHarness, ServiceContainer]:
    """Wire the reference router, hooks, and container onto a simulator harness."""
    conn_mgr = WebSocketConnectionManager()
    container = build_container(conn_mgr)
    recorded = instances if instances is not None else []

    def recording_factory(
        route_factory: cabc.Callable[..., WebSocketResource],
    ) -> WebSocketResource:
        """Create the route resource through the container and keep it."""
        instance = container.create_resource(route_factory)
        recorded.append(instance)
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
    return harness, container


def _resolve_feed(container: ServiceContainer) -> AnnouncementFeed:
    """Return the container's announcement feed, failing loudly when absent."""
    feed = container.resolve("announcement_feed")
    assert isinstance(feed, AnnouncementFeed), (
        "the container must provide the announcement feed service"
    )
    return feed


@given("the reference router with a recording factory", target_fixture="context")
def given_reference_router(event_loop: asyncio.AbstractEventLoop) -> ReferenceScenario:
    """Build the router wiring using the shared DI container."""
    instances: list[WebSocketResource] = []
    harness, container = _build_reference_harness(instances)
    return ReferenceScenario(
        harness=harness,
        container=container,
        feed=_resolve_feed(container),
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
    harness: SimulatorRouterHarness
    controller: WorkerController
    disconnecting: DisconnectingWebSocket
    atlas_messages: list[object] = dc.field(default_factory=list)
    zephyr_messages: list[object] = dc.field(default_factory=list)


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
    """Serve the app's router and worker alongside one disconnected recipient."""
    harness, container = _build_reference_harness()
    conn_mgr = typ.cast("WebSocketConnectionManager", container.resolve("conn_mgr"))
    feed = _resolve_feed(container)
    disconnecting = DisconnectingWebSocket()

    async def _prepare() -> WorkerController:
        """Register the failing recipient and start the real worker."""
        await conn_mgr.add_connection("atlas-gone", disconnecting)
        await conn_mgr.join_room("atlas-gone", _workspace_room("atlas"))
        controller = WorkerController()
        await controller.start(
            announcement_worker,
            conn_mgr=conn_mgr,
            announcement_feed=feed,
        )
        return controller

    controller = event_loop.run_until_complete(_prepare())
    try:
        yield AnnouncementScenario(
            loop=event_loop,
            harness=harness,
            controller=controller,
            disconnecting=disconnecting,
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


@when("clients in the atlas and zephyr workspaces create tasks")
def publish_announcements(announcement_scenario: AnnouncementScenario) -> None:
    """Drive real clients through the router so each workspace gets an announcement."""

    async def _drive() -> None:
        """Send task.add from each workspace and collect that client's frames."""
        for workspace, task_id, messages in (
            ("atlas", "T-42", announcement_scenario.atlas_messages),
            ("zephyr", "T-43", announcement_scenario.zephyr_messages),
        ):
            async with announcement_scenario.harness.connect(
                f"/workspaces/{workspace}/projects/triage/tasks",
                headers={"x-workspace-token": "seekrit", "x-user": "casey"},
                initial_inbound=[
                    (AddTask(task_id=task_id, title="Investigate delivery"), "json")
                ],
            ) as connection:
                # The resource replies task.added, then the workspace worker
                # broadcasts the announcement to the same client.
                async with asyncio.timeout(ANNOUNCEMENT_TIMEOUT):
                    await connection.wait_for_sent_count(2)
                messages.extend(connection.sent_messages)

    announcement_scenario.loop.run_until_complete(_drive())


@then("each workspace client receives its own announcement")
def then_healthy_recipients_receive(
    announcement_scenario: AnnouncementScenario,
) -> None:
    """Check that the failed recipient did not interrupt either workspace."""
    for messages, task_id in (
        (announcement_scenario.atlas_messages, "T-42"),
        (announcement_scenario.zephyr_messages, "T-43"),
    ):
        announcements: list[dict[str, object]] = [
            typ.cast("dict[str, object]", message)
            for message in messages
            if isinstance(message, dict) and message.get("type") == "announcement"
        ]
        assert len(announcements) == 1, (
            "each client must receive exactly one announcement"
        )
        payload = announcements[0]["payload"]
        assert isinstance(payload, dict), "the announcement must nest a payload mapping"
        assert payload["kind"] == "task_added", (
            "the announcement must report a task addition"
        )
        assert payload["task_id"] == task_id, (
            "the announcement must name the task created in that workspace"
        )
    assert announcement_scenario.disconnecting.messages == [], (
        "the disconnected recipient must not record a delivery"
    )
