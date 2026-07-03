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
from falcon_pachinko.testing import WebSocketSimulator
from falcon_pachinko.websocket import WebSocketConnectionManager

if typ.TYPE_CHECKING:  # pragma: no cover - typing helpers
    import falcon

    from examples.reference_app.services import AnnouncementFeed
    from falcon_pachinko import ServiceContainer, WebSocketResource, WebSocketRouter
else:  # pragma: no cover - runtime aliases for annotations
    AnnouncementFeed = typ.Any  # type: ignore[assignment]
    ServiceContainer = typ.Any  # type: ignore[assignment]
    WebSocketResource = typ.Any  # type: ignore[assignment]
    WebSocketRouter = typ.Any  # type: ignore[assignment]
    falcon = typ.Any  # type: ignore[assignment]


@dc.dataclass
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


class _RequestStub:
    def __init__(self, path: str, headers: dict[str, str]) -> None:
        self.path = path
        self.path_template = "/ws"
        self._headers = {key.lower(): value for key, value in headers.items()}

    def get_header(self, name: str, default: str | None = None) -> str | None:
        return self._headers.get(name.lower(), default)


@pytest.fixture
def event_loop() -> typ.Iterator[asyncio.AbstractEventLoop]:
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
    feed = typ.cast("AnnouncementFeed", container.resolve("announcement_feed"))
    instances: list[WebSocketResource] = []

    def recording_factory(
        route_factory: typ.Callable[..., WebSocketResource],
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
    req = _RequestStub(
        path="/ws/workspaces/atlas/projects/triage/tasks",
        headers={"x-workspace-token": "seekrit", "x-user": "casey"},
    )
    event_loop.run_until_complete(
        context.router.on_websocket(typ.cast("falcon.Request", req), context.simulator)
    )
    context.resource = _select_task_resource(context.instances)
    return context


@when('they send a "task.add" message for task "T-42"', target_fixture="context")
def when_send_task_add(
    context: ReferenceScenario, event_loop: asyncio.AbstractEventLoop
) -> ReferenceScenario:
    """Dispatch a schema-defined message through the active resource."""
    resource = typ.cast("TaskStreamResource", context.resource)
    payload = AddTask(task_id="T-42", title="Investigate event loop")
    raw = msjson.encode(payload)
    event_loop.run_until_complete(resource.dispatch(context.simulator, raw))
    context.last_event = event_loop.run_until_complete(context.feed.next_event())
    return context


@then("the connection is accepted")
def then_connection(context: ReferenceScenario) -> None:
    """Ensure the simulator recorded the handshake acceptance."""
    assert context.simulator.accepted is True


@then("the task stream resource replies with a task acknowledgement")
def then_acknowledgement(context: ReferenceScenario) -> None:
    """Check that the last frame is the expected acknowledgement."""
    message = typ.cast("dict[str, object]", context.simulator.sent_messages[-1])
    assert message["type"] == "task.added"


@then('the announcement feed captures an event for workspace "atlas"')
def then_feed_capture(context: ReferenceScenario) -> None:
    """Validate that the AnnouncementFeed observed the broadcast event."""
    assert context.last_event is not None
    workspace, payload = context.last_event
    assert workspace == "atlas"
    payload_dict = payload
    nested = typ.cast("dict[str, object]", payload_dict["payload"])
    assert nested["kind"] == "task_added"
