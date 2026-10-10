"""Tests for schema-driven dispatch using msgspec tagged unions."""

import asyncio
import re
import typing as typ

import msgspec as ms
import msgspec.json as msjson
import pytest

from falcon_pachinko import WebSocketLike, WebSocketResource, handles_message
from falcon_pachinko.unittests.helpers import DummyWS


class Join(ms.Struct, tag="join"):
    """Message structure for join events."""

    room: str


class Leave(ms.Struct, tag="leave"):
    """Message structure for leave events."""

    room: str


class IntegerMessage(ms.Struct, tag=1):
    """Message structure with an integer dispatch tag."""

    value: str


class Untagged(ms.Struct):
    """Message structure without a dispatch tag."""

    value: str


class ZeroTagMessage(ms.Struct, tag=0):
    """Message structure whose tag is a falsy, but valid, integer."""

    value: str


class EmptyTagMessage(ms.Struct, tag=""):
    """Message structure whose tag is a falsy, but valid, empty string."""

    value: str


class IntegerTaggedMessage(ms.Struct, tag=11):
    """Message structure with an integer tag, for building mixed-kind schemas."""

    value: str


class StringTaggedMessage(ms.Struct, tag="eleven"):
    """Message structure with a string tag, for building mixed-kind schemas."""

    value: str


# msgspec accepts either tag kind, but rejects a union combining them. Nothing
# validates this assignment: only ``schema`` attributes declared on a
# ``WebSocketResource`` subclass are checked, which is what the runtime
# assignment regression below relies on.
MixedTagUnion = IntegerTaggedMessage | StringTaggedMessage

MessageUnion = Join | Leave


class SchemaResource(WebSocketResource):
    """Resource using a schema for automatic message dispatch."""

    schema = MessageUnion

    def __init__(self) -> None:
        """Initialize with an empty events list."""
        self.events: list[tuple[str, typ.Any]] = []

    @handles_message("join")
    async def handle_join(self, ws: WebSocketLike, payload: Join) -> None:
        """Record join events."""
        self.events.append(("join", payload.room))

    @handles_message("leave")
    async def handle_leave(self, ws: WebSocketLike, payload: Leave) -> None:
        """Record leave events."""
        self.events.append(("leave", payload.room))

    async def on_unhandled(self, ws: WebSocketLike, message: str | bytes) -> None:
        """Record fallback messages."""
        self.events.append(("raw", message))


class IntegerTagResource(WebSocketResource):
    """Resource with a conventional handler for an integer tag."""

    schema = IntegerMessage

    def __init__(self) -> None:
        """Initialize with an empty events list."""
        self.events: list[tuple[str, str | bytes]] = []

    async def on_1(self, ws: WebSocketLike, payload: IntegerMessage) -> None:
        """Record the conventional integer-tag handler event."""
        self.events.append(("on_1", payload.value))

    async def on_unhandled(self, ws: WebSocketLike, message: str | bytes) -> None:
        """Record fallback messages."""
        self.events.append(("raw", message))


class RegisteredIntegerTagResource(WebSocketResource):
    """Resource with a registered handler and a conventional handler."""

    schema = IntegerMessage

    def __init__(self) -> None:
        """Initialize with an empty events list."""
        self.events: list[tuple[str, str | bytes]] = []

    @handles_message("integer")
    async def handle_integer(self, ws: WebSocketLike, payload: IntegerMessage) -> None:
        """Record the registered handler event."""
        self.events.append(("registered", payload.value))

    async def on_1(self, ws: WebSocketLike, payload: IntegerMessage) -> None:
        """Record the conventional handler event."""
        self.events.append(("on_1", payload.value))

    async def on_unhandled(self, ws: WebSocketLike, message: str | bytes) -> None:
        """Record fallback messages."""
        self.events.append(("raw", message))


class UntaggedFallbackResource(WebSocketResource):
    """Resource with handlers that must not match an untagged schema."""

    def __init__(self) -> None:
        """Initialize with an empty events list."""
        self.events: list[tuple[str, str | bytes]] = []

    async def on_none(self, ws: WebSocketLike, payload: Untagged) -> None:
        """Record the handler that would match a stringified ``None`` tag."""
        self.events.append(("on_none", payload.value))

    async def on_unhandled(self, ws: WebSocketLike, message: str | bytes) -> None:
        """Record fallback messages."""
        self.events.append(("raw", message))


class MixedTagResource(WebSocketResource):
    """Resource whose schema mixes tag kinds, applied at runtime.

    No ``schema`` attribute is declared here, so class creation performs no
    validation. The mixed union is assigned to the instance in the test
    instead, mirroring the runtime assignment that defeats the eager check.
    """

    def __init__(self) -> None:
        """Initialize with an empty events list."""
        self.events: list[tuple[str, str | bytes]] = []

    async def on_unhandled(self, ws: WebSocketLike, message: str | bytes) -> None:
        """Record fallback messages that must never be reached."""
        self.events.append(("raw", message))


class ZeroTagResource(WebSocketResource):
    """Resource with a conventional handler for a falsy integer tag."""

    schema = ZeroTagMessage

    def __init__(self) -> None:
        """Initialize with an empty events list."""
        self.events: list[tuple[str, str | bytes]] = []

    async def on_0(self, ws: WebSocketLike, payload: ZeroTagMessage) -> None:
        """Record the conventional handler for the zeroth tag."""
        self.events.append(("on_0", payload.value))

    async def on_unhandled(self, ws: WebSocketLike, message: str | bytes) -> None:
        """Record fallback messages."""
        self.events.append(("raw", message))


class EmptyTagResource(WebSocketResource):
    """Resource with a conventional handler for a falsy empty-string tag."""

    schema = EmptyTagMessage

    def __init__(self) -> None:
        """Initialize with an empty events list."""
        self.events: list[tuple[str, str | bytes]] = []

    async def on_unhandled(self, ws: WebSocketLike, message: str | bytes) -> None:
        """Record fallback messages."""
        self.events.append(("raw", message))


@pytest.mark.asyncio
async def test_schema_dispatch_to_handlers() -> None:
    """Messages matching the schema are routed to decorated handlers."""
    r = SchemaResource()
    r.bind_default_hook_manager()
    await r.dispatch(DummyWS(), msjson.encode(Join(room="a")))
    await r.dispatch(DummyWS(), msjson.encode(Leave(room="b")))
    assert r.events == [
        ("join", "a"),
        ("leave", "b"),
    ], "both schema-tagged messages should dispatch to their handlers in order"


@pytest.mark.asyncio
async def test_integer_tag_dispatches_to_conventional_handler() -> None:
    """Integer tags should be stringified for conventional handler lookup."""
    r = IntegerTagResource()
    r.bind_default_hook_manager()
    await r.dispatch(DummyWS(), msjson.encode(IntegerMessage(value="one")))
    assert r.events == [("on_1", "one")], (
        "integer tags should dispatch only to the on_1 conventional handler"
    )


@pytest.mark.asyncio
async def test_registered_handler_precedes_integer_tag_fallback() -> None:
    """Registered payload handlers should precede conventional tag lookup."""
    r = RegisteredIntegerTagResource()
    r.bind_default_hook_manager()
    await r.dispatch(DummyWS(), msjson.encode(IntegerMessage(value="one")))
    assert r.events == [("registered", "one")], (
        "the registered handler should run without the conventional fallback"
    )


@pytest.mark.asyncio
async def test_none_tag_calls_fallback_without_conventional_handler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """None tags should call on_unhandled rather than on_none."""
    r = UntaggedFallbackResource()
    r.bind_default_hook_manager()
    monkeypatch.setattr(r, "schema", Untagged)
    raw = msjson.encode(Untagged(value="untagged"))
    await r.dispatch(DummyWS(), raw)
    assert r.events == [("raw", raw)], "on_none must not run for a None tag"


@pytest.mark.asyncio
async def test_schema_unknown_tag_calls_fallback() -> None:
    """Unknown tags invoke the fallback handler with the raw message."""
    r = SchemaResource()
    r.bind_default_hook_manager()
    raw = msjson.encode({"type": "oops", "room": "x"})
    await r.dispatch(DummyWS(), raw)
    assert r.events == [("raw", raw)], "unknown tags should fall back to on_unhandled"


@pytest.mark.asyncio
async def test_schema_decode_error_calls_fallback() -> None:
    """Decode failures also trigger the fallback handler."""
    r = SchemaResource()
    r.bind_default_hook_manager()
    await r.dispatch(DummyWS(), b"not json")
    assert r.events == [
        ("raw", b"not json"),
    ], "decode failures should fall back to on_unhandled with the raw payload"


def test_invalid_schema_type_raises() -> None:
    """Only msgspec.Struct types are allowed in ``schema``."""

    class Good(ms.Struct, tag="good"):
        pass

    class Bad:
        pass

    with pytest.raises(
        TypeError, match=re.escape("schema must contain only msgspec.Struct types")
    ):

        class BadResource(WebSocketResource):
            schema = Good | Bad


def test_untagged_schema_struct_raises() -> None:
    """Every msgspec.Struct in ``schema`` must declare a dispatch tag."""

    class Untagged(ms.Struct):
        pass

    with pytest.raises(TypeError, match="schema Struct types must define a tag"):

        class BadResource(WebSocketResource):
            schema = Untagged


def test_duplicate_payload_type_raises() -> None:
    """Handlers with the same payload type should not be allowed."""

    class Payload(ms.Struct, tag="dup"):
        val: int

    with pytest.raises(ValueError, match="Duplicate payload type") as exc:

        class BadResource(WebSocketResource):
            schema = Payload

            @handles_message("a")
            async def h1(self, ws: WebSocketLike, payload: Payload) -> None: ...

            @handles_message("b")
            async def h2(self, ws: WebSocketLike, payload: Payload) -> None: ...

    assert "Payload" in str(exc.value), "error should name the duplicated payload type"
    assert "BadResource.h2" in str(exc.value), (
        "error should name the second handler that caused the duplicate"
    )


def test_mixed_tag_kinds_raise() -> None:
    """A schema must not mix integer-tagged and string-tagged Structs.

    msgspec rejects such a union, but only when it first derives the type at
    decode time, so it must be caught at class creation instead.
    """
    with pytest.raises(
        TypeError, match="schema tags must all be strings or all be integers"
    ):

        class BadResource(WebSocketResource):
            schema = IntegerTaggedMessage | StringTaggedMessage


def test_class_name_sentinel_mixed_with_integer_tag_raises() -> None:
    """``tag=True`` supplies a string tag, so it cannot mix with integer tags."""

    class ClassNameTagged(ms.Struct, tag=True):
        pass

    with pytest.raises(
        TypeError, match="schema tags must all be strings or all be integers"
    ):

        class BadResource(WebSocketResource):
            schema = ClassNameTagged | IntegerTaggedMessage


def test_homogeneous_integer_tags_are_accepted() -> None:
    """An all-integer tag schema is valid and still dispatches conventionally."""

    class Peer(ms.Struct, tag=12):
        value: str

    class Resource(WebSocketResource):
        schema = IntegerMessage | Peer

        def __init__(self) -> None:
            self.events: list[tuple[str, str | bytes]] = []

        async def on_1(self, ws: WebSocketLike, payload: IntegerMessage) -> None:
            """Record the conventional integer-tag handler event."""
            self.events.append(("on_1", payload.value))

        async def on_12(self, ws: WebSocketLike, payload: Peer) -> None:
            """Record the second conventional integer-tag handler event."""
            self.events.append(("on_12", payload.value))

        async def on_unhandled(self, ws: WebSocketLike, message: str | bytes) -> None:
            """Record fallback messages."""
            self.events.append(("raw", message))

    r = Resource()
    r.bind_default_hook_manager()
    raw = msjson.encode(Peer(value="peer"))
    asyncio.run(r.dispatch(DummyWS(), raw))
    assert r.events == [("on_12", "peer")], (
        "homogeneous integer-tag schemas must validate and dispatch"
    )


@pytest.mark.asyncio
async def test_runtime_assigned_mixed_tag_union_surfaces_the_error() -> None:
    """A mixed union assigned after class creation must fail visibly.

    Class-creation validation cannot see this schema. Swallowing msgspec's
    ``TypeError`` here would send every frame to ``on_unhandled`` while leaving
    the broken schema installed, so the configuration error must propagate
    instead of being disguised as a malformed message.
    """
    r = MixedTagResource()
    r.bind_default_hook_manager()
    # Assigning the invalid schema after class creation bypasses the eager
    # check, which is the point of the regression.
    r.schema = MixedTagUnion  # ty: ignore[invalid-assignment]  # deliberately bypasses validation
    raw = msjson.encode(IntegerTaggedMessage(value="eleven"))
    with pytest.raises(TypeError, match=r"both .int. and .str. tags"):
        await r.dispatch(DummyWS(), raw)
    assert not r.events, "an invalid schema must not be reported as a bad frame"


@pytest.mark.asyncio
async def test_zero_tag_dispatches_to_conventional_handler() -> None:
    """A falsy integer tag still reaches its conventional handler."""
    r = ZeroTagResource()
    r.bind_default_hook_manager()
    await r.dispatch(DummyWS(), msjson.encode(ZeroTagMessage(value="zero")))
    assert r.events == [("on_0", "zero")], (
        "tag=0 must dispatch rather than being mistaken for an absent tag"
    )


@pytest.mark.asyncio
async def test_empty_string_tag_falls_back_without_conventional_handler() -> None:
    """An empty-string tag has no conventional handler but must not raise."""
    r = EmptyTagResource()
    r.bind_default_hook_manager()
    raw = msjson.encode(EmptyTagMessage(value="empty"))
    await r.dispatch(DummyWS(), raw)
    assert r.events == [("raw", raw)], (
        "tag='' must fall back cleanly rather than raising TypeError"
    )
