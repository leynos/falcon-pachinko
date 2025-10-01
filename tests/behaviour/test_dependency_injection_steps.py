"""Behavioural tests for router-level dependency injection."""

from __future__ import annotations

import asyncio
import dataclasses
import typing as typ

from pytest_bdd import given, scenario, then, when

from falcon_pachinko import WebSocketResource, WebSocketRouter


class DummyWebSocket:
    """Minimal websocket stub recording lifecycle calls."""

    def __init__(self) -> None:
        self.closed = False
        self.accepted = False
        self.close_code: int | None = None

    async def accept(self, subprotocol: str | None = None) -> None:  # pragma: no cover - not exercised
        """Record that the connection was accepted."""
        self.accepted = True

    async def close(self, code: int = 1000) -> None:
        """Record that the connection was closed."""
        self.closed = True
        self.close_code = code


class InjectedChild(WebSocketResource):
    """Child resource capturing injected service dependencies."""

    instances: typ.ClassVar[list["InjectedChild"]] = []

    def __init__(self, *, service: str) -> None:
        self.service = service
        self.params: dict[str, object] = {}
        InjectedChild.instances.append(self)

    async def on_connect(self, req: object, ws: object, **params: object) -> bool:
        self.params = params
        return False


class InjectedParent(WebSocketResource):
    """Parent resource that exposes a nested child route."""

    instances: typ.ClassVar[list["InjectedParent"]] = []

    def __init__(self, *, label: str, service: str) -> None:
        self.label = label
        self.service = service
        self.params: dict[str, object] = {}
        InjectedParent.instances.append(self)
        self.add_subroute("child/{member}", InjectedChild)

    def get_child_context(self) -> dict[str, object]:
        """Return constructor kwargs for child resources."""
        return {"service": self.service}

    async def on_connect(self, req: object, ws: object, **params: object) -> bool:
        self.params = params
        return False


@dataclasses.dataclass
class RouterScenario:
    """Hold contextual state shared between steps."""

    router: WebSocketRouter
    loop: asyncio.AbstractEventLoop
    service: str
    websocket: DummyWebSocket | None = None
    parent: InjectedParent | None = None
    child: InjectedChild | None = None


def _resource_factory(service: str) -> typ.Callable[[typ.Callable[[], WebSocketResource]], WebSocketResource]:
    """Build a router-level resource factory injecting ``service``."""

    def build_resource(
        route_factory: typ.Callable[[], WebSocketResource]
    ) -> WebSocketResource:
        target = getattr(route_factory, "func", route_factory)
        args = getattr(route_factory, "args", ())
        base_kwargs = dict(getattr(route_factory, "keywords", {}) or {})
        base_kwargs["service"] = service
        return target(*args, **base_kwargs)

    return build_resource


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
    router = WebSocketRouter(resource_factory=_resource_factory(service))
    router.add_route("/rooms/{room}", InjectedParent, kwargs={"label": "rooms"})
    router.mount("/")
    loop = asyncio.new_event_loop()
    return RouterScenario(router=router, loop=loop, service=service)


@when(
    'a websocket connection targets "/rooms/alpha/child/beta"',
    target_fixture="context",
)
def when_dispatch(context: RouterScenario) -> RouterScenario:
    """Dispatch a connection through the router to the nested child route."""
    req = type(
        "Req",
        (),
        {"path": "/rooms/alpha/child/beta", "path_template": ""},
    )()
    ws = DummyWebSocket()
    context.loop.run_until_complete(context.router.on_websocket(req, ws))
    context.websocket = ws
    context.parent = InjectedParent.instances[-1]
    context.child = InjectedChild.instances[-1]
    return context


@then('the parent resource receives the "svc" dependency')
def then_parent(context: RouterScenario) -> None:
    """Assert that the parent instance received the injected service."""
    assert context.parent is not None
    assert context.parent.service == context.service
    assert context.parent.label == "rooms"


@then('the child resource receives the "svc" dependency')
def then_child(context: RouterScenario) -> None:
    """Assert that the child instance received the injected service."""
    assert context.child is not None
    assert context.child.service == context.service
    assert context.child.params == {"room": "alpha", "member": "beta"}


@then("the connection attempt is rejected")
def then_rejected(context: RouterScenario) -> None:
    """Ensure the websocket was closed instead of accepted and clean up."""
    assert context.websocket is not None
    try:
        assert context.websocket.closed is True
        assert context.websocket.accepted is False
    finally:
        context.loop.close()
