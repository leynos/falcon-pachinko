"""Tests for nested resource composition."""

import typing
from types import SimpleNamespace

import falcon
import pytest

from falcon_pachinko import WebSocketResource, WebSocketRouter
from falcon_pachinko.unittests.helpers import DummyWS


class Child(WebSocketResource):
    """Capture parameters passed to ``on_connect``."""

    instances: typing.ClassVar[list["Child"]] = []

    def __init__(self) -> None:
        """Record instance creation."""
        Child.instances.append(self)

    async def on_connect(self, req: object, ws: object, **params: object) -> bool:
        """Store connection params."""
        self.params = params
        return False


class Parent(WebSocketResource):
    """Parent resource with nested subroute."""

    def __init__(self) -> None:
        """Register subroute."""
        self.add_subroute("child/{cid}", Child)

    async def on_connect(self, req: object, ws: object, **params: object) -> bool:
        """Store parameters and refuse the connection."""
        self.params = params
        return False


@pytest.mark.asyncio
async def test_nested_subroute_params() -> None:
    """Parameters from each route level are merged."""
    Child.instances.clear()
    router = WebSocketRouter()
    router.add_route("/parent/{pid}", Parent)
    router.mount("/")
    req = typing.cast(
        "falcon.Request",
        SimpleNamespace(path="/parent/1/child/2", path_template=""),
    )
    await router.on_websocket(req, DummyWS())

    assert Child.instances[-1].params == {"pid": "1", "cid": "2"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("path", "description"),
    [
        ("/parent/1/oops", "Unmatched nested path should raise HTTPNotFound"),
        ("/parent/1child/2", "Missing slash between segments should not match"),
    ],
    ids=["unmatched_path", "malformed_path"],
)
async def test_nested_subroute_not_found(path: str, description: str) -> None:
    """Test cases where nested routes should raise HTTPNotFound."""
    router = WebSocketRouter()
    router.add_route("/parent/{pid}", Parent)
    router.mount("/")
    req = typing.cast(
        "falcon.Request",
        SimpleNamespace(path=path, path_template=""),
    )
    with pytest.raises(falcon.HTTPNotFound):
        await router.on_websocket(req, DummyWS())


def test_add_subroute_invalid_resource() -> None:
    """add_subroute must reject non-callables."""
    r = WebSocketResource()
    with pytest.raises(TypeError):
        r.add_subroute("child", typing.cast("typing.Any", object()))


class ContextChild(WebSocketResource):
    """Resource that receives context from its parent."""

    instances: typing.ClassVar[list["ContextChild"]] = []

    def __init__(self, project: str) -> None:
        """Record project and track instance."""
        self.project = project
        ContextChild.instances.append(self)

    async def on_connect(self, req: object, ws: object, **params: object) -> bool:
        """Mark that the child handled the connection."""
        self.state["child"] = True
        return False


class ContextParent(WebSocketResource):
    """Parent that injects context and shares state."""

    instances: typing.ClassVar[list["ContextParent"]] = []

    def __init__(self) -> None:
        """Register child subroute, track instance, and seed state."""
        self.project = "acme"
        self.state["parent"] = True
        self.add_subroute("child", ContextChild)
        ContextParent.instances.append(self)

    def get_child_context(self) -> dict[str, object]:
        """Provide constructor kwargs for the child."""
        return {"project": self.project}

    async def on_connect(self, req: object, ws: object, **params: object) -> bool:
        """No-op connect handler for tests."""
        return False


async def _setup_and_run_nested_test(
    child_class: type[typing.Any],
    parent_class: type[typing.Any],
    route_path: str,
    request_path: str,
) -> tuple[typing.Any, typing.Any]:
    """Execute nested resource flow and return created instances."""
    child_class.instances.clear()
    parent_class.instances.clear()
    router = WebSocketRouter()
    router.add_route(route_path, parent_class)
    router.mount("/")
    req = typing.cast(
        "falcon.Request",
        SimpleNamespace(path=request_path, path_template=""),
    )
    await router.on_websocket(req, DummyWS())
    parent = parent_class.instances[-1]
    child = child_class.instances[-1]
    return parent, child


@pytest.mark.asyncio
async def test_context_passed_and_state_shared() -> None:
    """Parent-supplied context and state propagate to the child."""
    parent, child = await _setup_and_run_nested_test(
        ContextChild, ContextParent, "/ctx", "/ctx/child"
    )
    assert child.project == "acme"
    assert child.state is parent.state
    assert child.state == {"parent": True, "child": True}


class InjectedChild(WebSocketResource):
    """Resource that mutates its own state."""

    instances: typing.ClassVar[list["InjectedChild"]] = []

    def __init__(self) -> None:
        """Track instances for inspection."""
        InjectedChild.instances.append(self)

    async def on_connect(self, req: object, ws: object, **params: object) -> bool:
        """Mark that the child handled the connection."""
        self.state["child"] = True
        return False


class InjectingParent(WebSocketResource):
    """Parent that injects custom state into the child."""

    instances: typing.ClassVar[list["InjectingParent"]] = []

    def __init__(self) -> None:
        """Register child subroute and seed parent state."""
        self.state["parent"] = True
        self.add_subroute("child", InjectedChild)
        InjectingParent.instances.append(self)

    def get_child_context(self) -> dict[str, object]:
        """Provide a fresh state mapping for the child."""
        return {"state": {"injected": True}}

    async def on_connect(self, req: object, ws: object, **params: object) -> bool:
        """No-op connect handler for tests."""
        return False


@pytest.mark.asyncio
async def test_state_injected_via_context() -> None:
    """Explicit state injection should override the parent's state."""
    parent, child = await _setup_and_run_nested_test(
        InjectedChild, InjectingParent, "/inj", "/inj/child"
    )
    assert child.state is not parent.state
    assert child.state == {"injected": True, "child": True}
    assert parent.state == {"parent": True}
