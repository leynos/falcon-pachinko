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
    """A WebSocket resource for testing message handling and fallback behavior."""

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


async def echo_handler(
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


async def raw_handler(self: RawResource, ws: WebSocketLike, payload: object) -> None:
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
    raw = msjson.encode({"type": "echo", "payload": {"text": "hi"}})
    await r.dispatch(DummyWS(), raw)
    assert r.seen == ["hi"]
    assert not r.fallback


@pytest.mark.asyncio
async def test_dispatch_unknown_type_calls_fallback() -> None:
    """Test that dispatching a message with an unknown type invokes the fallback
    handler.

    Verifies that when a message with an unregistered type is dispatched to
    EchoResource, the raw message is appended to the resource's fallback list.
    """
    r = EchoResource()
    raw = msjson.encode({"type": "unknown", "payload": {"text": "oops"}})
    await r.dispatch(DummyWS(), raw)
    assert r.fallback == [raw]


@pytest.mark.asyncio
async def test_handler_shared_across_instances() -> None:
    """Test that handlers are shared across instances of the same resource class."""
    r1 = EchoResource()
    r2 = EchoResource()
    raw = msjson.encode({"type": "echo", "payload": {"text": "hey"}})
    await r1.dispatch(DummyWS(), raw)
    await r2.dispatch(DummyWS(), raw)
    assert r1.seen == ["hey"]
    assert r2.seen == ["hey"]


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
    """Tests that RawResource receives the raw payload as-is when no payload type is
    specified.

    Verifies that the received list contains the exact payload passed, or None if
    the payload is missing.
    """
    r = RawResource()
    msg: dict[str, typ.Any] = {"type": "raw"}
    if payload != "MISSING":
        msg["payload"] = payload
    raw = msjson.encode(msg)
    await r.dispatch(DummyWS(), raw)
    assert r.received == [expected]


@pytest.mark.asyncio
async def test_invalid_payload_calls_fallback() -> None:
    """Test that an invalid payload type causes the message to be handled by the
    fallback method.

    Sends a message with an incorrect payload type to EchoResource and verifies
    that it is appended to the fallback list and not processed by the registered
    handler.
    """
    r = EchoResource()
    raw = msjson.encode({"type": "echo", "payload": {"text": 42}})
    await r.dispatch(DummyWS(), raw)
    assert r.fallback == [raw]
    assert not r.seen


@pytest.mark.asyncio
async def test_invalid_envelope_type_calls_fallback() -> None:
    """Non-string ``type`` fields trigger the fallback handler."""
    r = EchoResource()
    raw = msjson.encode({"type": 123, "payload": {"text": "hi"}})
    await r.dispatch(DummyWS(), raw)
    assert r.fallback == [raw]
    assert not r.seen


@pytest.mark.asyncio
async def test_extra_fields_strict_true_calls_fallback() -> None:
    """Extra fields trigger fallback when strict is True."""
    r = StrictResource()
    raw = msjson.encode({"type": "extra", "payload": {"val": 1, "extra": 2}})
    await r.dispatch(DummyWS(), raw)
    assert r.fallback == [raw]
    assert not r.seen


@pytest.mark.asyncio
async def test_extra_fields_strict_false_processed() -> None:
    """Extra fields are ignored when strict=False."""
    r = LenientResource()
    raw = msjson.encode({"type": "extra", "payload": {"val": 3, "extra": 4}})
    await r.dispatch(DummyWS(), raw)
    assert r.seen == [3]
    assert not r.fallback


@pytest.mark.asyncio
async def test_on_tag_dispatch_envelope() -> None:
    """Messages with matching ``on_{tag}`` handlers are dispatched."""
    r = ConventionalResource()
    raw = msjson.encode({"type": "echo", "payload": {"x": 1}})
    await r.dispatch(DummyWS(), raw)
    assert r.seen == [{"x": 1}]


@pytest.mark.asyncio
async def test_on_tag_camel_case() -> None:
    """CamelCase tags are converted to snake_case."""
    r = CamelResource()
    raw = msjson.encode(CamelResource.SendMessage(text="hi"))
    await r.dispatch(DummyWS(), raw)
    assert r.messages == ["hi"]


@pytest.mark.asyncio
async def test_sync_handler_ignored_and_fallback_behavior() -> None:
    """Synchronous ``on_{tag}`` handlers are ignored by dispatch."""
    r = SyncHandlerResource()
    raw = msjson.encode({"type": "sync", "payload": {"val": 1}})
    await r.dispatch(DummyWS(), raw)
    # The sync handler should not be called
    assert r.seen == []
    assert r.fallback == [raw]


@pytest.mark.asyncio
async def test_state_defaults_to_empty_dict() -> None:
    """Each resource instance starts with an empty state mapping."""
    r = EchoResource()
    assert isinstance(r.state, dict)
    assert not r.state
    r.state["foo"] = "bar"
    assert r.state["foo"] == "bar"


@pytest.mark.asyncio
async def test_state_custom_mapping_supported() -> None:
    """The state attribute can be swapped for any mutable mapping."""
    r = EchoResource()
    custom: dict[str, int] = {"count": 1}
    r.state = custom
    r.state["count"] += 1
    assert custom["count"] == 2


@pytest.mark.asyncio
async def test_state_is_unique_per_instance() -> None:
    """Resource instances do not share state by default."""
    r1 = EchoResource()
    r2 = EchoResource()
    r1.state["foo"] = "bar"
    assert "foo" not in r2.state


@pytest.mark.asyncio
async def test_state_rejects_non_mapping() -> None:
    """Assigning non-mapping to ``state`` raises ``TypeError``."""
    r = EchoResource()
    with pytest.raises(TypeError):
        r.state = 123  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_state_accepts_mapping_subclass() -> None:
    """Valid ``MutableMapping`` subclasses are accepted."""
    r = EchoResource()
    custom = collections.defaultdict(int)
    r.state = custom
    assert r.state is custom
    r.state["count"] += 1
    assert custom["count"] == 1
