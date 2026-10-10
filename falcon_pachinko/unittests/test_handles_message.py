"""Tests for the @handles_message decorator functionality.

This module contains comprehensive tests for the @handles_message decorator,
which is used to register message handlers for WebSocket resources. The tests
cover various scenarios including:

- Basic decorator functionality and message dispatching
- Error handling for duplicate handlers and invalid signatures
- Inheritance behaviour with parent and child resources
- Method overriding with and without decorators
- Type annotation handling for payload parameters
"""

from __future__ import annotations

import functools

import msgspec as ms
import msgspec.json as msjson
import pytest

from falcon_pachinko import (
    WebSocketLike,
    WebSocketResource,
    handles_message,
)
from falcon_pachinko.exceptions import (
    DuplicateHandlerRegistrationError,
    HandlerSignatureError,
    ReservedHandlerRegistrationError,
)
from falcon_pachinko.handlers import (
    LIFECYCLE_CALLBACK_NAMES,
    get_payload_type,
    is_lifecycle_callback,
)
from falcon_pachinko.unittests.helpers import DummyWS


class PingPayload(ms.Struct):
    """A simple message payload structure for testing ping messages."""

    text: str


async def _class_payload_handler(
    self: object, ws: WebSocketLike, payload: PingPayload
) -> None:
    """Provide a module-level handler with a class payload annotation."""


async def _generic_payload_handler(
    self: object, ws: WebSocketLike, payload: list[int]
) -> None:
    """Provide a module-level handler with a generic payload annotation."""


class DecoratedResource(WebSocketResource):
    """A WebSocket resource with a decorated message handler for testing."""

    def __init__(self) -> None:
        """Initialize the resource with an empty list to track seen messages."""
        self.seen: list[str] = []

    @handles_message("ping")
    async def handle_ping(self, ws: WebSocketLike, payload: PingPayload) -> None:
        """Handle ping messages by recording the text payload.

        Parameters
        ----------
        ws : WebSocketLike
            The WebSocket connection
        payload : PingPayload
            The ping message payload containing text
        """
        self.seen.append(payload.text)


@pytest.mark.asyncio
async def test_decorator_registers_handler() -> None:
    """Test that the @handles_message decorator properly registers a message handler.

    This test verifies that:
    1. A decorated method is registered as a handler for the specified message type
    2. The handler is correctly invoked when a matching message is dispatched
    3. The payload is properly deserialized and passed to the handler
    """
    r = DecoratedResource()
    r.bind_default_hook_manager()
    raw = msjson.encode({"type": "ping", "payload": {"text": "hi"}})
    await r.dispatch(DummyWS(), raw)
    assert r.seen == ["hi"], "decorated handler should record the ping payload text"


def test_decorator_registers_resolved_payload_type() -> None:
    """The handler registry retains the resolved payload class."""
    assert DecoratedResource.handlers["ping"].payload_type is PingPayload, (
        "the registry should retain the handler's resolved payload type"
    )


def test_duplicate_handler_raises() -> None:
    """Test that registering duplicate handlers for the same message type raises error.

    This test ensures that attempting to register multiple handlers for the same
    message type results in a RuntimeError with an appropriate error message.
    """
    with pytest.raises(DuplicateHandlerRegistrationError, match="Duplicate handler"):

        class BadResource(  # pyright: ignore[reportUnusedClass]  # class exists only to trigger the error
            WebSocketResource
        ):
            @handles_message("dup")
            async def h1(self, ws: WebSocketLike, payload: object) -> None: ...

            @handles_message("dup")
            async def h2(self, ws: WebSocketLike, payload: object) -> None: ...


def test_missing_payload_param_raises() -> None:
    """Test that handlers missing the required payload parameter raise a TypeError.

    This test verifies that the decorator validates handler method signatures
    and raises an error when the required payload parameter is missing.
    """
    with pytest.raises(TypeError):

        class BadSig(  # pyright: ignore[reportUnusedClass]  # class exists only to trigger the error
            WebSocketResource
        ):
            @handles_message(  # pyright: ignore[reportArgumentType]  # deliberately bad signature under test
                "oops"
            )
            async def bad(self, ws: WebSocketLike) -> None: ...


def test_ambiguous_payload_param_raises() -> None:
    """Handler with multiple annotated params should raise an error."""
    with pytest.raises(HandlerSignatureError):

        class Ambiguous(  # pyright: ignore[reportUnusedClass]  # class exists only to trigger the error
            WebSocketResource
        ):
            @handles_message("amb")
            async def bad(
                self,
                ws: WebSocketLike,
                first: int,
                second: str,
            ) -> None: ...


def test_decorating_lifecycle_callback_is_rejected() -> None:
    """A decorated lifecycle callback cannot be registered for a tag.

    Registering ``on_disconnect`` would put a lifecycle callback in the
    peer-reachable registry, which is the hole this guards.
    """
    with pytest.raises(ReservedHandlerRegistrationError, match="on_disconnect"):

        class DecoratedDisconnect(  # pyright: ignore[reportUnusedClass]  # class exists only to trigger the error
            WebSocketResource
        ):
            @handles_message("disconnect")
            async def on_disconnect(
                self, ws: WebSocketLike, payload: object
            ) -> None: ...


def test_decorating_lifecycle_name_under_unrelated_tag_is_rejected() -> None:
    """The reserved name, not the tag, is what rejects the registration.

    The signature is deliberately handler-shaped here so the reserved-name
    check is the one under test; ``on_connect``'s real lifecycle signature is
    rejected earlier by the decorator's own signature validation.
    """
    with pytest.raises(ReservedHandlerRegistrationError, match="on_connect"):

        class DecoratedConnect(  # pyright: ignore[reportUnusedClass]  # class exists only to trigger the error
            WebSocketResource
        ):
            @handles_message("anything")
            async def on_connect(self, ws: WebSocketLike, payload: object) -> None: ...


def test_decorating_inherited_lifecycle_callback_is_rejected() -> None:
    """A subclass cannot register the callback under a new method name.

    The child defines no ``on_disconnect`` of its own, so identity against
    the parent's implementation is the only thing that can reject this; a
    name-only check would let the inherited callback into the registry.
    """
    with pytest.raises(ReservedHandlerRegistrationError, match="on_disconnect"):
        LifecycleParent.add_handler("bye", LifecycleParent.on_disconnect, strict=False)


def test_add_handler_rejects_partial_lifecycle_callback() -> None:
    """A ``functools.partial`` wrapper does not launder a lifecycle callback.

    A partial exposes neither the wrapped function's ``__name__`` nor its
    identity, so without unwrapping it would read as an ordinary handler
    while still invoking ``on_disconnect`` when a peer frame selected it.
    """

    class ManualResource(WebSocketResource):
        """Resource used to exercise manual registration."""

        async def on_disconnect(self, ws: WebSocketLike, close_code: int) -> None:
            """Stand in for the lifecycle callback under test."""

    wrapped = functools.partial(ManualResource.on_disconnect)

    with pytest.raises(ReservedHandlerRegistrationError, match="on_disconnect"):
        ManualResource.add_handler("disconnect", wrapped, strict=False)


def test_add_handler_rejects_lifecycle_callback() -> None:
    """``add_handler`` refuses a callback that implements a lifecycle method."""

    class ManualResource(WebSocketResource):
        """Resource used to exercise manual registration."""

        async def on_disconnect(self, ws: WebSocketLike, close_code: int) -> None:
            """Stand in for the lifecycle callback under test."""

    with pytest.raises(ReservedHandlerRegistrationError, match="on_disconnect"):
        ManualResource.add_handler("disconnect", ManualResource.on_disconnect)


def test_add_handler_accepts_ordinary_handler() -> None:
    """An ordinary coroutine remains registrable through ``add_handler``."""

    class ManualResource(WebSocketResource):
        """Resource used to exercise manual registration."""

        async def on_disconnect(self, ws: WebSocketLike, close_code: int) -> None:
            """Stand in for the lifecycle callback under test."""

    async def handle_note(
        self: ManualResource, ws: WebSocketLike, payload: object
    ) -> None:
        """Stand in for an application message handler."""

    ManualResource.add_handler("disconnect", handle_note, payload_type=None)

    assert ManualResource.handlers["disconnect"].handler is handle_note, (
        "an ordinary handler stays registrable under a reserved tag string"
    )


class LifecycleParent(WebSocketResource):
    """Parent resource supplying a lifecycle callback to its children."""

    async def on_disconnect(self, ws: WebSocketLike, close_code: int) -> None:
        """Stand in for the lifecycle callback under test."""


def test_lifecycle_callback_names_cover_the_base_class_hooks() -> None:
    """The reserved set names exactly the base class's lifecycle hooks.

    ``WebSocketResource`` defines three ``on_*`` coroutine callbacks that are
    not message handlers. If a fourth is added, this test should fail so the
    set is extended deliberately rather than left behind.
    """
    base_hooks = {
        name
        for name in dir(WebSocketResource)
        if name.startswith("on_") and callable(getattr(WebSocketResource, name, None))
    }
    assert base_hooks == set(LIFECYCLE_CALLBACK_NAMES), (
        f"the reserved set must match the base class hooks: {sorted(base_hooks)}"
    )


def test_lifecycle_predicate_recognises_every_rejected_shape() -> None:
    """The predicate accepts each way a lifecycle callback can be presented."""
    partial_disconnect = functools.partial(LifecycleParent.on_disconnect)

    class Aliased(WebSocketResource):
        async def on_disconnect(self, ws: WebSocketLike, close_code: int) -> None:
            """Stand in for the lifecycle callback under test."""

        on_bye = on_disconnect

    class Child(LifecycleParent):
        """Inherit the parent's callback without redefining it."""

    async def ordinary(self: object, ws: WebSocketLike, payload: object) -> None:
        """Stand in for an ordinary message handler."""

    async def on_unhandled(self: object, ws: WebSocketLike, payload: object) -> None:
        """Stand in for the fallback with a handler-shaped signature."""

    # ``@handles_message`` returns a descriptor, and the descriptor itself is
    # callable-shaped only through ``__get__``. The predicate unwraps it so a
    # descriptor presented directly is judged on the function it registers.
    descriptor = handles_message("bye")(on_unhandled)

    rejected = {
        "direct": LifecycleParent.on_disconnect,
        "alias": Aliased.__dict__["on_bye"],
        "inherited": Child.on_disconnect,
        "partial": partial_disconnect,
        "nested partial": functools.partial(partial_disconnect),
        "decorated descriptor": descriptor,
    }
    for label, callable_ in rejected.items():
        assert is_lifecycle_callback(LifecycleParent, callable_), (
            f"{label} must be recognised as a lifecycle callback"
        )
    assert not is_lifecycle_callback(LifecycleParent, ordinary), (
        "an ordinary coroutine must not be classified as a lifecycle callback"
    )
    assert not is_lifecycle_callback(LifecycleParent, 42), (
        "a non-callable must not be classified as a lifecycle callback"
    )


class ParentResource(WebSocketResource):
    """A parent WebSocket resource class with a decorated message handler.

    This class is used to test inheritance behaviour of message handlers.
    """

    @handles_message("parent")
    async def parent(self, ws: WebSocketLike, payload: object) -> None:
        """Handle parent messages.

        Parameters
        ----------
        ws : WebSocketLike
            The WebSocket connection
        payload : object
            The message payload
        """


class ChildResource(ParentResource):
    """A child WebSocket resource that inherits from ParentResource.

    This class tests that:
    1. Child classes inherit parent message handlers
    2. Child classes can define their own additional handlers
    3. Child classes can override parent handlers without decoration
    """

    def __init__(self) -> None:
        """Initialize the child resource with a list to track invoked handlers."""
        self.invoked: list[str] = []

    @handles_message("child")
    async def child(self, ws: WebSocketLike, payload: object) -> None:
        """Handle child-specific messages.

        Parameters
        ----------
        ws : WebSocketLike
            The WebSocket connection
        payload : object
            The message payload
        """
        self.invoked.append("child")

    async def parent(  # pyright: ignore[reportIncompatibleVariableOverride]  # intentional undecorated override for the test
        self, ws: WebSocketLike, payload: object
    ) -> None:
        """Override the parent handler to record invocation.

        Parameters
        ----------
        ws : WebSocketLike
            The WebSocket connection
        payload : object
            The message payload
        """
        # override to record
        self.invoked.append("parent")


class RedecoratedChild(ParentResource):
    """Re-register the parent's message type on a child resource."""

    @handles_message("parent")
    async def parent(self, ws: WebSocketLike, payload: object) -> None:
        """Handle the parent's message type on the child resource."""


def test_child_handler_registry_is_isolated() -> None:
    """Adding a child handler does not mutate the parent registry."""
    assert ChildResource.handlers is not ParentResource.handlers, (
        "a child resource should have its own handler registry"
    )
    assert "child" not in ParentResource.handlers, (
        "the child handler should not appear in the parent registry"
    )
    assert "child" in ChildResource.handlers, (
        "the child registry should include its decorated handler"
    )


def test_none_handlers_mapping_is_replaced_before_registration() -> None:
    """A class-body None mapping is replaced with a fresh handler registry."""

    class NoneHandlersResource(WebSocketResource):
        """A resource that asks the descriptor to initialise its registry."""

        handlers = None  # pyright: ignore[reportAssignmentType]  # tests None setup

        @handles_message("fresh")
        async def handle_fresh(self, ws: WebSocketLike, payload: PingPayload) -> None:
            """Handle the fresh-registry test message."""

    handlers = NoneHandlersResource.handlers
    assert isinstance(handlers, dict), (
        "the descriptor should replace the None class attribute with a mapping"
    )
    assert handlers is not ParentResource.handlers, (
        "the None class attribute should become a fresh child mapping"
    )
    assert "fresh" in handlers, "the fresh mapping should contain the decorated handler"


class DecoratedOverride(ParentResource):
    """A resource that overrides a parent handler using the decorator.

    This class tests that child classes can override parent handlers
    by re-decorating methods with the same message type.
    """

    @handles_message("parent")
    async def parent(self, ws: WebSocketLike, payload: object) -> None:
        """Override the parent handler with decoration.

        Parameters
        ----------
        ws : WebSocketLike
            The WebSocket connection
        payload : object
            The message payload
        """
        self.invoked = "decorated"


@pytest.mark.asyncio
async def test_handlers_inherited() -> None:
    """Test that child classes inherit message handlers from parent classes.

    This test verifies that:
    1. Child classes inherit decorated handlers from parent classes
    2. Child classes can define their own additional handlers
    3. Both inherited and child-specific handlers work correctly
    4. Method overrides without decoration still work as handlers
    """
    r = ChildResource()
    r.bind_default_hook_manager()
    await r.dispatch(DummyWS(), msjson.encode({"type": "parent"}))
    await r.dispatch(DummyWS(), msjson.encode({"type": "child"}))
    assert r.invoked == [
        "parent",
        "child",
    ], "both the overridden parent and the new child handler should run"


@pytest.mark.asyncio
async def test_decorated_override() -> None:
    """Test that child classes can override parent handlers using decoration.

    This test verifies that when a child class re-decorates a method with
    the same message type as a parent handler, the child's handler takes
    precedence over the parent's handler.
    """
    r = DecoratedOverride()
    r.bind_default_hook_manager()
    await r.dispatch(DummyWS(), msjson.encode({"type": "parent"}))
    assert r.invoked == "decorated", "re-decorated handler should override the parent"


def test_get_payload_type_returns_class_annotation() -> None:
    """Return a handler's resolved class annotation unchanged."""
    assert get_payload_type(_class_payload_handler) is PingPayload, (
        "the resolver should return a class payload annotation as-is"
    )


def test_get_payload_type_preserves_non_class_annotation() -> None:
    """Return a non-class typing object without runtime validation."""
    assert get_payload_type(_generic_payload_handler) == list[int], (
        "the resolver should preserve a parameterized generic annotation"
    )


def test_child_handler_registry_contains_inherited_and_child_types() -> None:
    """The child registry includes both inherited and child message types."""
    assert {"parent", "child"} <= ChildResource.handlers.keys(), (
        "the child mapping should include inherited and child handlers"
    )


def test_child_can_redecorate_parent_handler() -> None:
    """A child can register its own handler for the parent's message type."""
    assert (
        RedecoratedChild.handlers["parent"].handler
        is not ParentResource.handlers["parent"].handler
    ), "re-decorating the parent's message type should register the child handler"


def test_unresolved_annotation_is_ignored() -> None:
    """Test that unresolved type annotations are handled gracefully.

    This test verifies that when a handler method has a payload parameter
    with a type annotation that cannot be resolved at runtime, the system
    gracefully handles this by setting the payload type to None in the
    handler registry, rather than raising an error.
    """

    class UnknownAnnoResource(WebSocketResource):
        """A resource with an unresolved payload type annotation."""

        @handles_message("unknown")
        async def handler(
            self,
            ws: WebSocketLike,
            payload: UnknownPayload,  # ty: ignore[unresolved-reference]  # ruff: ignore[undefined-name]  # deliberately unresolved to test the fallback
        ) -> None:
            """Handle messages with unresolved payload type.

            Parameters
            ----------
            ws : WebSocketLike
                The WebSocket connection
            payload : UnknownPayload
                The message payload with unresolved type
            """

    assert UnknownAnnoResource.handlers["unknown"].payload_type is None, (
        "unresolved annotations should fall back to a None payload type"
    )


def test_unannotated_payload_type_is_none() -> None:
    """A handler without a payload annotation registers no payload type."""

    class UnannotatedPayloadResource(WebSocketResource):
        """A resource whose handler intentionally omits its payload type."""

        def __init__(self) -> None:
            self.received: object | None = None

        @handles_message("unannotated")
        async def handle_unannotated(
            self,
            ws: WebSocketLike,
            payload,  # pyright: ignore[reportUnknownParameterType, reportMissingParameterType]  # ruff: ignore[missing-type-function-argument]  # deliberately unannotated
        ) -> None:
            """Handle the unannotated-payload test message."""
            self.received = payload

    assert UnannotatedPayloadResource.handlers["unannotated"].payload_type is None, (
        "an unannotated payload should register as None"
    )
