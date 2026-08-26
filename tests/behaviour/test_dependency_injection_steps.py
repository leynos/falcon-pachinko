"""Behavioural tests for router-level dependency injection."""

from __future__ import annotations

import dataclasses as dc
import typing as typ

import pytest
from pytest_bdd import given, scenario, then, when

from falcon_pachinko import WebSocketResource, WebSocketRouter
from falcon_pachinko.unittests.resource_factories import resource_factory
from tests._stubs import RecordingWebSocket

if typ.TYPE_CHECKING:
    import asyncio
    import collections.abc as cabc


@pytest.fixture
def event_loop(
    event_loop_policy: asyncio.AbstractEventLoopPolicy,
) -> cabc.Iterator[asyncio.AbstractEventLoop]:
    """Provide an event loop managed by pytest-asyncio's policy fixture."""
    loop = event_loop_policy.new_event_loop()
    try:
        yield loop
    finally:
        loop.close()


class InjectedChild(WebSocketResource):
    """Child resource capturing injected service dependencies."""

    instances: typ.ClassVar[list[InjectedChild]] = []

    def __init__(self, *, service: str) -> None:
        self.service = service
        self.params: dict[str, object] = {}
        InjectedChild.instances.append(self)

    async def on_connect(self, req: object, ws: object, **params: object) -> bool:
        """Record path params observed during connection negotiation."""
        self.params = params
        return False


class InjectedParent(WebSocketResource):
    """Parent resource that exposes a nested child route."""

    instances: typ.ClassVar[list[InjectedParent]] = []

    def __init__(self, *, label: str, service: str) -> None:
        self.label = label
        self.service = service
        self.params: dict[str, object] = {}
        InjectedParent.instances.append(self)
        self.add_subroute("child/{member}", InjectedChild)

    async def on_connect(self, req: object, ws: object, **params: object) -> bool:
        """Record path params observed during connection negotiation."""
        self.params = params
        return False


@dc.dataclass(slots=True)
class RouterScenario:
    """Hold contextual state shared between steps."""

    router: WebSocketRouter
    service: str
    websocket: RecordingWebSocket | None = None
    parent: InjectedParent | None = None
    child: InjectedChild | None = None


@scenario(
    "dependency_injection.feature",
    "route resources are constructed through the configured factory",
)
def test_dependency_injection() -> None:  # pragma: no cover - bdd registration
    """Scenario registration for dependency injection behaviour."""


@given(
    'a router configured with a resource factory injecting service "svc"',
    target_fixture="context",
)
def given_router() -> RouterScenario:
    """Create a router that injects a named service into resources."""
    InjectedParent.instances.clear()
    InjectedChild.instances.clear()
    service = "svc"
    router = WebSocketRouter(resource_factory=resource_factory(service))
    router.add_route("/rooms/{room}", InjectedParent, label="rooms")
    router.mount("/")
    return RouterScenario(router=router, service=service)


@when(
    'a websocket connection targets "/rooms/alpha/child/beta"',
    target_fixture="context",
)
def when_dispatch(
    context: RouterScenario, event_loop: asyncio.AbstractEventLoop
) -> RouterScenario:
    """Dispatch a connection through the router to the nested child route."""
    req = type(
        "Req",
        (),
        {"path": "/rooms/alpha/child/beta", "path_template": ""},
    )()
    ws = RecordingWebSocket()
    event_loop.run_until_complete(context.router.on_websocket(req, ws))
    context.websocket = ws
    context.parent = InjectedParent.instances[-1]
    context.child = InjectedChild.instances[-1]
    return context


@then('the parent resource receives the "svc" dependency')
def then_parent(context: RouterScenario) -> None:
    """Assert that the parent instance received the injected service."""
    assert context.parent is not None, "the parent resource must have been constructed"
    assert context.parent.service == context.service, (
        "the parent resource must receive the injected service"
    )
    assert context.parent.label == "rooms", "the parent resource must keep its label"


@then('the child resource receives the "svc" dependency')
def then_child(context: RouterScenario) -> None:
    """Assert that the child instance received the injected service."""
    assert context.child is not None, "the child resource must have been constructed"
    assert context.child.service == context.service, (
        "the child resource must receive the injected service"
    )
    assert context.child.params == {"room": "alpha", "member": "beta"}, (
        "the child resource must observe both nested path parameters"
    )


@then("the connection attempt is rejected")
def then_rejected(context: RouterScenario) -> None:
    """Ensure the websocket was closed instead of accepted."""
    assert context.websocket is not None, "a websocket must have been dispatched"
    assert context.websocket.closed is True, "the connection must be closed"
    assert context.websocket.accepted is False, "the connection must not be accepted"
    assert context.websocket.close_code == 1000, "the close code must be the default"
