"""Tests for WebSocketResource functionality."""

from __future__ import annotations

import collections
import typing as typ

import msgspec as ms
import msgspec.json as msjson
import pytest

from falcon_pachinko import WebSocketLike, WebSocketResource, handles_message
from falcon_pachinko.unittests.helpers import DummyWS


class EchoPayload(ms.Struct):
    """A simple message payload structure for testing echo messages."""

    text: str


class ExtraPayload(ms.Struct):
    """Payload used to test strict vs lenient conversion."""

    val: int


class EchoResource(WebSocketResource):
    """A WebSocket resource for testing message handling and fallback behaviour."""

    def __init__(self) -> None:
        """Initialize the EchoResource with empty lists.

        Initializes the EchoResource with empty lists for handled and fallback
        messages.

        The `seen` list stores texts from successfully handled payloads, while the
        `fallback` list records messages that do not match any registered handler
        or fail payload validation.
        """
        self.seen: list[typ.Any] = []
        self.fallback: list[typ.Any] = []

    async def on_unhandled(self, ws: WebSocketLike, message: str | bytes) -> None:
        """Handle messages that do not match any registered handler.

        Handles messages that do not match any registered handler by appending
        them to the fallback list.

        Parameters
        ----------
        ws : WebSocketLike
            The WebSocket connection instance
        message : str or bytes
            The raw message received, as a string or bytes
        """
        self.fallback.append(message)


async def echo_handler(  # ruff: ignore[unused-async]  # add_handler requires an async callable
    self: EchoResource, ws: WebSocketLike, payload: EchoPayload
) -> None:
    """Handle an "echo" message by recording the payload text.

    Appends the `text` field from the received `EchoPayload` to the resource's
    `seen` list.

    Parameters
    ----------
    self : EchoResource
        The resource instance
    ws : WebSocketLike
        The WebSocket connection instance
    payload : EchoPayload
        The echo message payload containing text
    """
    self.seen.append(payload.text)


EchoResource.add_handler("echo", echo_handler, payload_type=EchoPayload)


class RawResource(WebSocketResource):
    """A WebSocket resource for testing raw message handling."""

    def __init__(self) -> None:
        """Initialize the RawResource instance with an empty list.

        Initializes the RawResource instance with an empty list to store received
        messages or payloads.
        """
        self.received: list[typ.Any] = []

    async def on_unhandled(self, ws: WebSocketLike, message: str | bytes) -> None:
        """Handle incoming messages by appending them to the received list.

        This method acts as a fallback for messages that do not match any
        registered handler.

        Parameters
        ----------
        ws : WebSocketLike
            The WebSocket connection instance
        message : str or bytes
            The raw message received
        """
        self.received.append(message)


async def raw_handler(  # ruff: ignore[unused-async]  # add_handler requires an async callable
    self: RawResource, ws: WebSocketLike, payload: object
) -> None:
    """Handle incoming messages of type "raw".

    Handles incoming messages of type "raw" by appending the payload to the
    resource's received list.

    Parameters
    ----------
    self : RawResource
        The resource instance
    ws : WebSocketLike
        The WebSocket connection instance
    payload : typ.Any
        The raw payload received with the message. Can be any type, including
        None
    """
    self.received.append(payload)


RawResource.add_handler("raw", raw_handler, payload_type=None)


class ConventionalResource(WebSocketResource):
    """Resource used to test ``on_{tag}`` dispatch."""

    def __init__(self) -> None:
        self.seen: list[typ.Any] = []

    async def on_echo(self, ws: WebSocketLike, payload: object) -> None:
        """Record ``payload`` from ``echo`` messages."""
        self.seen.append(payload)


class CamelResource(WebSocketResource):
    """Resource testing CamelCase tag conversion."""

    class SendMessage(ms.Struct, tag="sendMessage"):
        """Payload for a send message."""

        text: str

    schema = SendMessage

    def __init__(self) -> None:
        self.messages: list[str] = []

    async def on_send_message(self, ws: WebSocketLike, payload: SendMessage) -> None:
        """Record ``payload`` text from ``sendMessage`` messages."""
        self.messages.append(payload.text)


class SyncHandlerResource(WebSocketResource):
    """Resource with a synchronous ``on_{tag}`` handler."""

    def __init__(self) -> None:
        self.seen: list[typ.Any] = []
        self.fallback: list[str | bytes] = []

    def on_sync(
        self, ws: WebSocketLike, payload: object
    ) -> None:  # pragma: no cover - ignored by dispatch
        """Ignore synchronous handler used for testing."""
        self.seen.append(payload)

    async def on_unhandled(self, ws: WebSocketLike, message: str | bytes) -> None:
        """Record fallback messages."""
        self.fallback.append(message)


class StrictResource(WebSocketResource):
    """Resource with strict payload conversion (default)."""

    def __init__(self) -> None:
        self.seen: list[int] = []
        self.fallback: list[str | bytes] = []

    async def on_unhandled(self, ws: WebSocketLike, message: str | bytes) -> None:
        """Record messages that fail validation."""
        self.fallback.append(message)

    @handles_message("extra")
    async def handle_extra(self, ws: WebSocketLike, payload: ExtraPayload) -> None:
        """Record validated payload values."""
        self.seen.append(payload.val)


class LifecycleResource(WebSocketResource):
    """Resource whose lifecycle callbacks must not be peer-selectable.

    The overrides record every lifecycle call so a test can prove that no
    reserved tag reached them while the ordinary handler kept working.
    """

    def __init__(self) -> None:
        self.lifecycle: list[str] = []
        self.fallback: list[str | bytes] = []
        self.messages: list[object] = []

    async def on_connect(
        self, req: object, ws: WebSocketLike, **params: object
    ) -> bool:
        """Record the connect decision requested by the lifecycle."""
        self.lifecycle.append("connect")
        return True

    async def on_disconnect(self, ws: WebSocketLike, close_code: int) -> None:
        """Record the close code the lifecycle supplied."""
        self.lifecycle.append(f"disconnect:{close_code}")

    async def on_unhandled(self, ws: WebSocketLike, message: str | bytes) -> None:
        """Record the original frame handed to the fallback."""
        self.fallback.append(message)

    async def on_ping(self, ws: WebSocketLike, payload: object) -> None:
        """Record payloads from ``ping`` messages."""
        self.messages.append(payload)


class InheritedLifecycleResource(LifecycleResource):
    """Child resource inheriting the parent's lifecycle callbacks."""

    def __init__(self) -> None:
        """Initialize the child's own event log alongside the parent's."""
        super().__init__()
        self.child_messages: list[object] = []

    async def on_child(self, ws: WebSocketLike, payload: object) -> None:
        """Record payloads from ``child`` messages."""
        self.child_messages.append(payload)


class AliasedLifecycleResource(WebSocketResource):
    """Resource that aliases ``on_disconnect`` under a dispatachable name."""

    def __init__(self) -> None:
        self.lifecycle: list[int] = []
        self.fallback: list[str | bytes] = []

    async def on_disconnect(self, ws: WebSocketLike, close_code: int) -> None:
        """Record the close code the lifecycle supplied."""
        self.lifecycle.append(close_code)

    on_bye = on_disconnect

    async def on_unhandled(self, ws: WebSocketLike, message: str | bytes) -> None:
        """Record the original frame handed to the fallback."""
        self.fallback.append(message)


class InheritedAliasLifecycleResource(AliasedLifecycleResource):
    """Child resource inheriting an alias of a lifecycle callback."""


class DecoratedReservedTagResource(WebSocketResource):
    """Resource that reuses a reserved tag on an ordinary method."""

    def __init__(self) -> None:
        self.lifecycle: list[int] = []
        self.messages: list[object] = []

    async def on_disconnect(self, ws: WebSocketLike, close_code: int) -> None:
        """Record the close code the lifecycle supplied."""
        self.lifecycle.append(close_code)

    @handles_message("disconnect")
    async def handle_disconnect(self, ws: WebSocketLike, payload: object) -> None:
        """Record payloads sent to the reserved tag ``disconnect``."""
        self.messages.append(payload)


class PunctuationResource(WebSocketResource):
    """Resource whose handler name only matches after normalisation."""

    def __init__(self) -> None:
        self.seen: list[object] = []

    async def on_send_message(self, ws: WebSocketLike, payload: object) -> None:
        """Record payloads from dotted tags."""
        self.seen.append(payload)


class LenientResource(WebSocketResource):
    """Resource with lenient payload conversion (allows extra fields)."""

    def __init__(self) -> None:
        self.seen: list[int] = []
        self.fallback: list[str | bytes] = []

    async def on_unhandled(self, ws: WebSocketLike, message: str | bytes) -> None:
        """Record messages that fail validation."""
        self.fallback.append(message)

    @handles_message("extra", strict=False)
    async def handle_extra(self, ws: WebSocketLike, payload: ExtraPayload) -> None:
        """Record validated payload values."""
        self.seen.append(payload.val)


@pytest.mark.asyncio
async def test_dispatch_calls_registered_handler() -> None:
    """Test that dispatching a message with a registered type calls the handler."""
    r = EchoResource()
    r.bind_default_hook_manager()
    raw = msjson.encode({"type": "echo", "payload": {"text": "hi"}})
    await r.dispatch(DummyWS(), raw)
    assert r.seen == ["hi"], "registered handler should record the echo payload text"
    assert not r.fallback, "a registered handler should not fall back"


@pytest.mark.asyncio
async def test_dispatch_unknown_type_calls_fallback() -> None:
    """Unknown message types invoke the fallback handler.

    Verifies that when a message with an unregistered type is dispatched to
    EchoResource, the raw message is appended to the resource's fallback list.
    """
    r = EchoResource()
    r.bind_default_hook_manager()
    raw = msjson.encode({"type": "unknown", "payload": {"text": "oops"}})
    await r.dispatch(DummyWS(), raw)
    assert r.fallback == [raw], "unregistered message types should reach on_unhandled"


@pytest.mark.asyncio
async def test_handler_shared_across_instances() -> None:
    """Test that handlers are shared across instances of the same resource class."""
    r1 = EchoResource()
    r2 = EchoResource()
    r1.bind_default_hook_manager()
    r2.bind_default_hook_manager()
    raw = msjson.encode({"type": "echo", "payload": {"text": "hey"}})
    await r1.dispatch(DummyWS(), raw)
    await r2.dispatch(DummyWS(), raw)
    assert r1.seen == ["hey"], "shared handler should record on the first instance"
    assert r2.seen == ["hey"], "shared handler should record on the second instance"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"text": "hi"}, {"text": "hi"}),
        (None, None),
        ("MISSING", None),
    ],
)
async def test_payload_type_none_passes_raw(payload: object, expected: object) -> None:
    """RawResource receives the raw payload as-is when no payload type is specified.

    Verifies that the received list contains the exact payload passed, or None if
    the payload is missing.
    """
    r = RawResource()
    r.bind_default_hook_manager()
    msg: dict[str, typ.Any] = {"type": "raw"}
    if payload != "MISSING":
        msg["payload"] = payload
    raw = msjson.encode(msg)
    await r.dispatch(DummyWS(), raw)
    assert r.received == [
        expected,
    ], "the raw payload should pass through unchanged when no payload type is set"


@pytest.mark.asyncio
async def test_invalid_payload_calls_fallback() -> None:
    """An invalid payload type causes the message to be handled by the fallback method.

    Sends a message with an incorrect payload type to EchoResource and verifies
    that it is appended to the fallback list and not processed by the registered
    handler.
    """
    r = EchoResource()
    r.bind_default_hook_manager()
    raw = msjson.encode({"type": "echo", "payload": {"text": 42}})
    await r.dispatch(DummyWS(), raw)
    assert r.fallback == [raw], "invalid payload should be routed to on_unhandled"
    assert not r.seen, "the registered handler should not run for an invalid payload"


@pytest.mark.asyncio
async def test_invalid_envelope_type_calls_fallback() -> None:
    """Non-string ``type`` fields trigger the fallback handler."""
    r = EchoResource()
    r.bind_default_hook_manager()
    raw = msjson.encode({"type": 123, "payload": {"text": "hi"}})
    await r.dispatch(DummyWS(), raw)
    assert r.fallback == [
        raw,
    ], "a non-string type field should be routed to on_unhandled"
    assert not r.seen, "the registered handler should not run for a bad envelope type"


@pytest.mark.asyncio
async def test_extra_fields_strict_true_calls_fallback() -> None:
    """Extra fields trigger fallback when strict is True."""
    r = StrictResource()
    r.bind_default_hook_manager()
    raw = msjson.encode({"type": "extra", "payload": {"val": 1, "extra": 2}})
    await r.dispatch(DummyWS(), raw)
    assert r.fallback == [raw], "extra fields should fall back when strict is True"
    assert not r.seen, "the handler should not run when validation fails"


@pytest.mark.asyncio
async def test_extra_fields_strict_false_processed() -> None:
    """Extra fields are ignored when strict=False."""
    r = LenientResource()
    r.bind_default_hook_manager()
    raw = msjson.encode({"type": "extra", "payload": {"val": 3, "extra": 4}})
    await r.dispatch(DummyWS(), raw)
    assert r.seen == [3], "extra fields should be ignored when strict is False"
    assert not r.fallback, "a lenient conversion should not fall back"


@pytest.mark.asyncio
async def test_on_tag_dispatch_envelope() -> None:
    """Messages with matching ``on_{tag}`` handlers are dispatched."""
    r = ConventionalResource()
    r.bind_default_hook_manager()
    raw = msjson.encode({"type": "echo", "payload": {"x": 1}})
    await r.dispatch(DummyWS(), raw)
    assert r.seen == [{"x": 1}], "on_echo should receive the decoded payload"


@pytest.mark.asyncio
async def test_on_tag_camel_case() -> None:
    """CamelCase tags are converted to snake_case."""
    r = CamelResource()
    r.bind_default_hook_manager()
    raw = msjson.encode(CamelResource.SendMessage(text="hi"))
    await r.dispatch(DummyWS(), raw)
    assert r.messages == ["hi"], "camelCase tag should route to the snake_case handler"


@pytest.mark.asyncio
async def test_sync_handler_ignored_and_fallback_behaviour() -> None:
    """Synchronous ``on_{tag}`` handlers are ignored by dispatch."""
    r = SyncHandlerResource()
    r.bind_default_hook_manager()
    raw = msjson.encode({"type": "sync", "payload": {"val": 1}})
    await r.dispatch(DummyWS(), raw)
    assert not r.seen, "the synchronous handler should not be called by dispatch"
    assert r.fallback == [raw], "a sync-only handler should be treated as unhandled"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "tag",
    ["connect", "disconnect", "unhandled", "Disconnect", "DISCONNECT"],
)
async def test_reserved_tags_never_reach_lifecycle_callback(tag: str) -> None:
    """Reserved tags fall back without invoking a lifecycle callback.

    The case variants matter because the convention normalises the
    discriminator before lookup, so ``Disconnect`` and ``DISCONNECT`` both
    reduce to the reserved name.
    """
    r = LifecycleResource()
    r.bind_default_hook_manager()
    raw = msjson.encode({"type": tag, "payload": 1000})
    await r.dispatch(DummyWS(), raw)
    assert not r.lifecycle, (
        f"tag {tag!r} must not invoke a lifecycle callback: {r.lifecycle}"
    )
    assert r.fallback == [raw], (
        f"tag {tag!r} must reach on_unhandled with the original frame"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("tag", ["disconnect", "connect", "unhandled"])
async def test_inherited_lifecycle_callbacks_are_not_peer_selectable(tag: str) -> None:
    """Lifecycle callbacks inherited from a parent cannot be selected."""
    r = InheritedLifecycleResource()
    r.bind_default_hook_manager()
    raw = msjson.encode({"type": tag, "payload": 1000})
    await r.dispatch(DummyWS(), raw)
    assert not r.lifecycle, (
        f"an inherited lifecycle callback must not run for {tag!r}: {r.lifecycle}"
    )
    assert r.fallback == [raw], "an inherited reserved name must fall back"


@pytest.mark.asyncio
@pytest.mark.parametrize("tag", ["bye", "Bye", "bYe"])
async def test_lifecycle_alias_is_not_peer_selectable(tag: str) -> None:
    """An alias bound to a lifecycle callback stays unreachable."""
    r = AliasedLifecycleResource()
    r.bind_default_hook_manager()
    raw = msjson.encode({"type": tag, "payload": 1000})
    await r.dispatch(DummyWS(), raw)
    assert not r.lifecycle, (
        f"the on_bye alias must not invoke on_disconnect for {tag!r}: {r.lifecycle}"
    )
    assert r.fallback == [raw], "an aliased lifecycle callback must fall back"


@pytest.mark.asyncio
async def test_inherited_lifecycle_alias_is_not_peer_selectable() -> None:
    """A child inherits the parent's alias without exposing it to peers."""
    r = InheritedAliasLifecycleResource()
    r.bind_default_hook_manager()
    raw = msjson.encode({"type": "bye", "payload": 1000})
    await r.dispatch(DummyWS(), raw)
    assert not r.lifecycle, (
        f"the inherited on_bye alias must not run on_disconnect: {r.lifecycle}"
    )
    assert r.fallback == [raw], "an inherited alias must fall back"


@pytest.mark.asyncio
async def test_reserved_tag_string_on_distinct_method_is_registered() -> None:
    """A reserved tag stays legal when a different method handles it."""
    r = DecoratedReservedTagResource()
    r.bind_default_hook_manager()
    raw = msjson.encode({"type": "disconnect", "payload": 1000})
    await r.dispatch(DummyWS(), raw)
    assert r.messages == [1000], (
        f"the decorated handler must receive the payload: {r.messages}"
    )
    assert not r.lifecycle, (
        f"the registered handler must not invoke on_disconnect: {r.lifecycle}"
    )


@pytest.mark.asyncio
async def test_punctuation_normalisation_still_resolves_handler() -> None:
    """Non-alphanumeric tag characters normalise to underscores."""
    r = PunctuationResource()
    r.bind_default_hook_manager()
    await r.dispatch(DummyWS(), msjson.encode({"type": "send.message", "payload": 7}))
    assert r.seen == [7], "send.message should resolve to on_send_message"


class DescriptorWrappedResource(WebSocketResource):
    """Resource whose ``on_*`` attributes are descriptor-wrapped coroutines.

    ``getattr_static`` returns the descriptor object rather than the function
    for these, so none of them satisfies the coroutine test the conventional
    dispatcher applies.
    """

    def __init__(self) -> None:
        self.fallback: list[str | bytes] = []

    @staticmethod
    async def on_static(resource: object, ws: WebSocketLike, payload: object) -> None:
        """Synchronous-attribute handler that must not be admitted."""

    @classmethod
    async def on_classm(cls, ws: WebSocketLike, payload: object) -> None:
        """Class-bound handler that must not be admitted."""

    @property
    def on_prop(self) -> object:
        """Descriptor that must not be run during class creation."""
        msg = "a peer tag must never evaluate this property"
        raise AssertionError(msg)

    async def on_unhandled(self, ws: WebSocketLike, message: str | bytes) -> None:
        """Record fallback messages."""
        self.fallback.append(message)


@pytest.mark.asyncio
@pytest.mark.parametrize("tag", ["static", "classm", "prop"])
async def test_descriptor_wrapped_attributes_are_not_dispatchable(tag: str) -> None:
    """Only a plain coroutine function is admitted to the allowlist."""
    r = DescriptorWrappedResource()
    r.bind_default_hook_manager()
    raw = msjson.encode({"type": tag, "payload": None})
    await r.dispatch(DummyWS(), raw)
    assert r.fallback == [raw], (
        f"a descriptor-wrapped attribute must fall back for {tag!r}: {r.fallback}"
    )


def test_conventional_registry_excludes_lifecycle_names() -> None:
    """The class registry lists message names and never lifecycle names."""
    names = LifecycleResource._conventional_handler_names
    assert "on_ping" in names, "an ordinary conventional handler stays registered"
    assert names.isdisjoint({"on_connect", "on_disconnect", "on_unhandled"}), (
        f"lifecycle names must not be dispatchable: {sorted(names)}"
    )
    assert WebSocketResource._conventional_handler_names == frozenset(), (
        "the base class must expose no conventional handlers"
    )


def test_state_defaults_to_empty_dict() -> None:
    """Each resource instance starts with an empty state mapping."""
    r = EchoResource()
    assert isinstance(r.state, dict), "state should default to a dict"
    assert not r.state, "a freshly created resource should have empty state"
    r.state["foo"] = "bar"
    assert r.state["foo"] == "bar", "state should be a mutable mapping"


def test_state_custom_mapping_supported() -> None:
    """The state attribute can be swapped for any mutable mapping."""
    r = EchoResource()
    custom: dict[str, int] = {"count": 1}
    r.state = custom
    r.state["count"] += 1
    assert custom["count"] == 2, "mutating r.state should mutate the assigned mapping"


def test_state_is_unique_per_instance() -> None:
    """Resource instances do not share state by default."""
    r1 = EchoResource()
    r2 = EchoResource()
    r1.state["foo"] = "bar"
    assert "foo" not in r2.state, "state should not be shared between instances"


@pytest.mark.parametrize(
    ("value", "case"),
    [(123, "scalar"), ([1, 2, 3], "sequence")],
    ids=["scalar", "sequence"],
)
def test_state_rejects_non_mapping(value: object, case: str) -> None:
    """Assigning a non-mapping to ``state`` raises ``TypeError``.

    The sequence case matters: a list supplies ``__getitem__``,
    ``__setitem__``, and ``__iter__``, so a method-probing check accepted it.
    """
    r = EchoResource()
    # The cast smuggles a deliberately non-mapping value past the signature
    # to exercise the runtime type check.
    with pytest.raises(TypeError, match="state must be a MutableMapping"):
        typ.cast("typ.Any", r).state = value


def test_state_accepts_mapping_subclass() -> None:
    """Valid ``MutableMapping`` subclasses are accepted."""
    r = EchoResource()
    custom = collections.defaultdict(int)
    r.state = custom
    assert r.state is custom, "assigning state should store the given mapping directly"
    r.state["count"] += 1
    assert custom["count"] == 1, "mutating r.state should mutate the assigned mapping"
