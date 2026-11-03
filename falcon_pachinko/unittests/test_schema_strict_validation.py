"""Tests for strict unknown-field validation in schema-mode dispatch.

Schema dispatch decodes frames generically so undeclared fields survive until
the strictness decision. These tests pin that contract: extras reach
``on_unhandled``, ``strict=False`` relaxes only the framework's shallow check
and msgspec's coercion, and nested Structs keep their own declarations.
"""

import collections.abc as cabc
import decimal
import typing as typ

import msgspec as ms
import msgspec.json as msjson
import pytest

from falcon_pachinko import WebSocketLike, WebSocketResource, handles_message
from falcon_pachinko.unittests.helpers import DummyWS
from falcon_pachinko.unittests.test_schema_dispatch import (
    IntegerTagResource,
    Join,
    MessageUnion,
    SchemaResource,
)
from falcon_pachinko.utils import ValidationError


class _RecordingResource(WebSocketResource):
    """Base resource recording handler events and fallback frames.

    Each test resource below declares only its ``schema`` and handlers; the
    record format lives in one place so it cannot drift between them.
    """

    def __init__(self) -> None:
        """Initialize with an empty events list."""
        self.events: list[object] = []

    async def on_unhandled(self, ws: WebSocketLike, message: str | bytes) -> None:
        """Record fallback frames."""
        self.events.append(("raw", message))


class _RecordingResourceLike(typ.Protocol):
    """Resource surface shared by the parametrized dispatch tests.

    The dispatch resources live in two modules; this lets one test drive both
    without pretending one class is the other. ``events`` is read-only here
    because the tests only inspect what was recorded.
    """

    @property
    def events(self) -> cabc.Sequence[object]:
        """Events recorded so far, in dispatch order."""

    def bind_default_hook_manager(self) -> object:
        """Install the framework's default hook manager."""

    async def dispatch(self, ws: WebSocketLike, raw: str | bytes) -> None:
        """Dispatch one received frame."""


class ConventionalJoinResource(_RecordingResource):
    """Schema-backed resource resolving a conventional ``on_join`` handler."""

    schema = Join

    async def on_join(self, ws: WebSocketLike, payload: Join) -> None:
        """Record the conventional handler event."""
        self.events.append(("on_join", payload.room))


class LenientSchemaResource(_RecordingResource):
    """Schema-backed resource whose decorated handler opts out of strictness."""

    schema = MessageUnion

    @handles_message("join", strict=False)
    async def handle_join(self, ws: WebSocketLike, payload: Join) -> None:
        """Record the join room while ignoring undeclared fields."""
        self.events.append(("join", payload.room))


class CustomTagMessage(ms.Struct, tag="custom", tag_field="kind"):
    """Message dispatched through a non-default tag field."""

    value: str


class CustomTagResource(_RecordingResource):
    """Resource resolving a conventional handler from a custom tag field."""

    schema = CustomTagMessage

    async def on_custom(self, ws: WebSocketLike, payload: CustomTagMessage) -> None:
        """Record the value carried by the custom tag message."""
        self.events.append(("on_custom", payload.value))


class RenamedMessage(ms.Struct, tag="renamed", rename="camel"):
    """Message whose wire field names differ from its attribute names."""

    room_name: str


class RenamedSchemaResource(_RecordingResource):
    """Resource dispatching a renamed message through a conventional handler."""

    schema = RenamedMessage

    async def on_renamed(self, ws: WebSocketLike, payload: RenamedMessage) -> None:
        """Record the room under its Python attribute name."""
        self.events.append(("on_renamed", payload.room_name))


class NestedItem(ms.Struct):
    """Item nested inside a schema message."""

    amount: int


class NestedMessage(ms.Struct, tag="nested"):
    """Message carrying one nested Struct and a list of them."""

    item: NestedItem
    items: list[NestedItem] = ms.field(default_factory=list)


class NestedResource(_RecordingResource):
    """Resource dispatching nested Structs through a conventional handler."""

    schema = NestedMessage

    async def on_nested(self, ws: WebSocketLike, payload: NestedMessage) -> None:
        """Record the nested amounts in declaration and container order."""
        self.events.append((
            "nested",
            payload.item.amount,
            [item.amount for item in payload.items],
        ))


class ClosedNestedItem(ms.Struct, forbid_unknown_fields=True):
    """Nested Struct that rejects fields beyond its declaration."""

    amount: int


class ClosedNestedMessage(ms.Struct, tag="closed_nested"):
    """Message with a nested Struct that forbids unknown fields."""

    item: ClosedNestedItem


class ClosedNestedResource(_RecordingResource):
    """Resource whose nested Struct owns its own unknown-field contract."""

    schema = ClosedNestedMessage

    async def on_closed_nested(
        self, ws: WebSocketLike, payload: ClosedNestedMessage
    ) -> None:
        """Record the nested amount."""
        self.events.append(("closed_nested", payload.item.amount))


class OptionalMessage(ms.Struct, tag="optional"):
    """Message with a defaulted and an explicitly nullable field."""

    label: str = "default"
    detail: str | None = None


class OptionalResource(_RecordingResource):
    """Resource dispatching a message with optional fields."""

    schema = OptionalMessage

    async def on_optional(self, ws: WebSocketLike, payload: OptionalMessage) -> None:
        """Record the resolved label and detail values."""
        self.events.append(("optional", payload.label, payload.detail))


class CoercedMessage(ms.Struct, tag="coerced"):
    """Message whose integer field msgspec can coerce from a JSON string."""

    count: int


class StrictCoercionResource(_RecordingResource):
    """Resource whose decorated handler keeps strict conversion."""

    schema = CoercedMessage

    @handles_message("coerced")
    async def handle_coerced(self, ws: WebSocketLike, payload: CoercedMessage) -> None:
        """Record the integer count."""
        self.events.append(("coerced", payload.count))


class LenientCoercionResource(_RecordingResource):
    """Resource whose decorated handler opts into lax conversion."""

    schema = CoercedMessage

    @handles_message("coerced", strict=False)
    async def handle_coerced(self, ws: WebSocketLike, payload: CoercedMessage) -> None:
        """Record the laxly coerced integer count."""
        self.events.append(("coerced", payload.count))


class WireTypesMessage(ms.Struct, tag="wire_types"):
    """Message whose fields only decode faithfully from the original frame."""

    scores: dict[int, str]
    blob: ms.Raw
    amount: decimal.Decimal


class WireTypesResource(_RecordingResource):
    """Resource receiving a message with wire-only field types."""

    schema = WireTypesMessage

    async def on_wire_types(self, ws: WebSocketLike, payload: WireTypesMessage) -> None:
        """Record the decoded mapping keys, raw JSON and exact decimal."""
        self.events.append((
            "wire_types",
            payload.scores,
            msjson.decode(payload.blob),
            payload.amount,
        ))


class LenientWireMessage(ms.Struct, tag="lenient_wire"):
    """Message mixing a coercible field with wire-only field types."""

    count: int
    scores: dict[int, str]
    blob: ms.Raw
    amount: decimal.Decimal


class LenientWireResource(_RecordingResource):
    """Resource whose lenient handler still receives wire-level values."""

    schema = LenientWireMessage

    @handles_message("lenient_wire", strict=False)
    async def handle_lenient_wire(
        self, ws: WebSocketLike, payload: LenientWireMessage
    ) -> None:
        """Record every mixed field after lax coercion."""
        self.events.append((
            "lenient_wire",
            payload.count,
            payload.scores,
            msjson.decode(payload.blob),
            payload.amount,
        ))


class ClosedTopMessage(ms.Struct, tag="closed_top", forbid_unknown_fields=True):
    """Top-level message that always forbids undeclared fields."""

    value: int


class LenientClosedTopResource(_RecordingResource):
    """Resource opting out of strictness for a closed top-level Struct."""

    schema = ClosedTopMessage

    @handles_message("closed_top", strict=False)
    async def handle_closed_top(
        self, ws: WebSocketLike, payload: ClosedTopMessage
    ) -> None:
        """Record the value of a closed message."""
        self.events.append(("closed_top", payload.value))


class RaisingHandlerResource(_RecordingResource):
    """Resource whose handler raises its own validation error."""

    schema = Join

    @handles_message("join")
    async def handle_join(self, ws: WebSocketLike, payload: Join) -> None:
        """Raise a handler-owned validation failure."""
        msg = "handler failed"
        raise ValidationError(msg)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "resource_type",
    [SchemaResource, ConventionalJoinResource],
    ids=["decorated", "conventional"],
)
async def test_schema_extra_field_calls_fallback(
    resource_type: type[_RecordingResourceLike],
) -> None:
    """An undeclared field rejects a schema message before the handler runs.

    Both registration styles keep the default strict contract: a decorated
    handler and a conventional ``on_<tag>`` handler see the same decision.
    """
    r = resource_type()
    r.bind_default_hook_manager()
    raw = msjson.encode({"type": "join", "room": "a", "extra": 1})
    await r.dispatch(DummyWS(), raw)
    assert r.events == [("raw", raw)], (
        "an extra top-level field must reach on_unhandled with the exact raw frame"
    )


@pytest.mark.asyncio
async def test_schema_strict_false_ignores_extra_field() -> None:
    """``strict=False`` delivers the Struct while ignoring the extra field."""
    r = LenientSchemaResource()
    r.bind_default_hook_manager()
    raw = msjson.encode({"type": "join", "room": "a", "extra": 1})
    await r.dispatch(DummyWS(), raw)
    assert r.events == [("join", "a")], (
        "a lenient handler should receive the typed Struct without the extra field"
    )


@pytest.mark.asyncio
async def test_schema_unknown_tag_after_generic_decode_calls_fallback() -> None:
    """A decoded but unrecognized tag reaches the fallback with the raw frame."""
    r = SchemaResource()
    r.bind_default_hook_manager()
    raw = msjson.encode({"type": "oops", "room": "x"})
    await r.dispatch(DummyWS(), raw)
    assert r.events == [("raw", raw)], (
        "an unrecognized tag must fall back with the original frame"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "frame",
    [
        pytest.param({"value": "one"}, id="missing-tag"),
        # ``bool`` subclasses ``int``, so a plain equality lookup would let
        # ``True`` select the ``tag=1`` member.
        pytest.param({"type": True, "value": "one"}, id="boolean-tag"),
        pytest.param([1, 2], id="array"),
        pytest.param("scalar", id="string"),
        pytest.param(7, id="integer"),
        pytest.param(None, id="null"),
        pytest.param(True, id="boolean"),
    ],
)
async def test_schema_unresolvable_frame_calls_fallback(frame: object) -> None:
    """Frames without a usable integer tag reach the fallback unchanged.

    A frame can fail to resolve because it is not a JSON object, omits the tag
    field, or carries a tag of the wrong kind. All three must fall back rather
    than fail dispatch or select a handler.
    """
    r = IntegerTagResource()
    r.bind_default_hook_manager()
    raw = msjson.encode(frame)
    await r.dispatch(DummyWS(), raw)
    assert r.events == [("raw", raw)], (
        "a frame without a usable integer tag must fall back with the raw frame"
    )


@pytest.mark.asyncio
async def test_schema_custom_tag_field_dispatches() -> None:
    """A Struct's declared tag field is used to resolve its handler."""
    r = CustomTagResource()
    r.bind_default_hook_manager()
    raw = msjson.encode({"kind": "custom", "value": "v"})
    await r.dispatch(DummyWS(), raw)
    assert r.events == [("on_custom", "v")], (
        "a custom tag field must resolve the member and its conventional handler"
    )


@pytest.mark.asyncio
async def test_schema_custom_tag_field_rejects_default_name() -> None:
    """The default ``type`` key is an ordinary field for a custom tag schema."""
    r = CustomTagResource()
    r.bind_default_hook_manager()
    raw = msjson.encode({"type": "custom", "value": "v"})
    await r.dispatch(DummyWS(), raw)
    assert r.events == [("raw", raw)], (
        "a frame without the declared tag field must fall back"
    )


@pytest.mark.asyncio
async def test_schema_renamed_field_accepts_encoded_name() -> None:
    """Strict validation accepts the encoded wire name declared by ``rename``."""
    r = RenamedSchemaResource()
    r.bind_default_hook_manager()
    raw = msjson.encode({"type": "renamed", "roomName": "lobby"})
    await r.dispatch(DummyWS(), raw)
    assert r.events == [("on_renamed", "lobby")], (
        "an encoded field name must satisfy strict validation"
    )


@pytest.mark.asyncio
async def test_schema_renamed_field_rejects_attribute_name() -> None:
    """The Python attribute name is not a valid wire name."""
    r = RenamedSchemaResource()
    r.bind_default_hook_manager()
    raw = msjson.encode({"type": "renamed", "room_name": "lobby"})
    await r.dispatch(DummyWS(), raw)
    assert r.events == [("raw", raw)], (
        "the Python attribute name must be rejected on the wire"
    )


@pytest.mark.asyncio
async def test_schema_member_decodes_wire_level_types() -> None:
    """The member decodes from the frame, not from the generic mapping.

    Converting the generically decoded mapping would stringify the integer
    mapping key, fail to capture ``Raw`` JSON, and round the decimal through a
    ``float`` before it reached ``Decimal``.
    """
    r = WireTypesResource()
    r.bind_default_hook_manager()
    raw = (
        b'{"type": "wire_types", "scores": {"1": "a"}, '
        b'"blob": {"nested": true}, '
        b'"amount": 0.12345678901234567890123456789}'
    )
    await r.dispatch(DummyWS(), raw)
    assert r.events == [
        (
            "wire_types",
            {1: "a"},
            {"nested": True},
            decimal.Decimal("0.12345678901234567890123456789"),
        )
    ], "wire-level values must survive into the typed payload unchanged"


@pytest.mark.asyncio
async def test_schema_lenient_coercion_keeps_wire_level_types() -> None:
    """Lax coercion must not cost the neighbouring wire-level values.

    A lenient handler needs msgspec's lax coercion for ``"5"`` in an integer
    field. Decoding the frame with the handler's strictness keeps that coercion
    while still preserving integer mapping keys, ``Raw`` JSON and the exact
    decimal, which a conversion of the generic mapping would degrade.
    """
    r = LenientWireResource()
    r.bind_default_hook_manager()
    raw = (
        b'{"type": "lenient_wire", "count": "5", "scores": {"1": "a"}, '
        b'"blob": {"nested": true}, '
        b'"amount": 0.12345678901234567890123456789}'
    )
    await r.dispatch(DummyWS(), raw)
    expected_event = (
        "lenient_wire",
        5,
        {1: "a"},
        {"nested": True},
        decimal.Decimal("0.12345678901234567890123456789"),
    )
    assert r.events == [expected_event], (
        "lax coercion must not degrade the other wire-level field values"
    )


@pytest.mark.asyncio
async def test_schema_nested_structs_ignore_extras_by_default() -> None:
    """Nested Structs keep their own default: undeclared nested fields drop.

    Strictness is shallow. The top-level mapping is checked for undeclared
    keys; whether a nested Struct rejects extras is that Struct's decision.
    """
    r = NestedResource()
    r.bind_default_hook_manager()
    raw = msjson.encode({
        "type": "nested",
        "item": {"amount": 1, "ignored": "x"},
        "items": [{"amount": 2, "ignored": True}],
    })
    await r.dispatch(DummyWS(), raw)
    assert r.events == [("nested", 1, [2])], (
        "nested Structs and containers must decode while ignoring their extras"
    )


@pytest.mark.asyncio
async def test_schema_nested_closed_struct_rejects_extras() -> None:
    """A nested Struct with ``forbid_unknown_fields=True`` rejects extras."""
    r = ClosedNestedResource()
    r.bind_default_hook_manager()
    raw = msjson.encode({
        "type": "closed_nested",
        "item": {"amount": 1, "extra": 2},
    })
    await r.dispatch(DummyWS(), raw)
    assert r.events == [("raw", raw)], (
        "a closed nested Struct must fail conversion and reach the fallback"
    )


@pytest.mark.asyncio
async def test_schema_omitted_and_null_fields_keep_their_semantics() -> None:
    """Omitted fields take defaults; an explicit JSON null stays ``None``."""
    omitted = OptionalResource()
    omitted.bind_default_hook_manager()
    await omitted.dispatch(DummyWS(), msjson.encode({"type": "optional"}))

    explicit_null = OptionalResource()
    explicit_null.bind_default_hook_manager()
    await explicit_null.dispatch(
        DummyWS(),
        msjson.encode({"type": "optional", "label": "set", "detail": None}),
    )

    assert omitted.events == [("optional", "default", None)], (
        "an omitted field should keep its declared default"
    )
    assert explicit_null.events == [("optional", "set", None)], (
        "an explicit null should stay None rather than taking the default"
    )


@pytest.mark.asyncio
async def test_schema_strict_handler_rejects_lax_coercion() -> None:
    """Default strict conversion rejects a string in an integer field."""
    r = StrictCoercionResource()
    r.bind_default_hook_manager()
    raw = msjson.encode({"type": "coerced", "count": "5"})
    await r.dispatch(DummyWS(), raw)
    assert r.events == [("raw", raw)], (
        "strict conversion must reject the lax coercion and fall back"
    )


@pytest.mark.asyncio
async def test_schema_strict_false_coerces_laxly() -> None:
    """``strict=False`` uses msgspec's lax coercion for the same frame."""
    r = LenientCoercionResource()
    r.bind_default_hook_manager()
    await r.dispatch(DummyWS(), msjson.encode({"type": "coerced", "count": "5"}))
    assert r.events == [("coerced", 5)], (
        "lax conversion should coerce the string to an integer"
    )


@pytest.mark.asyncio
async def test_schema_strict_false_still_honours_closed_member() -> None:
    """A member's ``forbid_unknown_fields=True`` outranks ``strict=False``.

    ``strict=False`` relaxes the framework's shallow key check and msgspec's
    coercion, not a Struct's own declaration.
    """
    r = LenientClosedTopResource()
    r.bind_default_hook_manager()
    raw = msjson.encode({"type": "closed_top", "value": 1, "extra": 2})
    await r.dispatch(DummyWS(), raw)
    assert r.events == [("raw", raw)], (
        "a closed Struct must reject extras even when the handler is lenient"
    )


@pytest.mark.asyncio
async def test_schema_handler_validation_error_propagates() -> None:
    """A ``ValidationError`` raised inside a handler is not a bad frame."""
    r = RaisingHandlerResource()
    r.bind_default_hook_manager()
    with pytest.raises(ValidationError, match="handler failed"):
        await r.dispatch(DummyWS(), msjson.encode({"type": "join", "room": "a"}))
    assert not r.events, "a handler failure must not reach on_unhandled"


def test_array_like_schema_member_raises() -> None:
    """A schema member encoded as a JSON array cannot be tag-dispatched."""

    class ArrayMessage(ms.Struct, tag="array", array_like=True):
        value: str

    with pytest.raises(TypeError, match="array_like"):

        class BadResource(WebSocketResource):
            schema = ArrayMessage
