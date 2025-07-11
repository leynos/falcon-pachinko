"""Tests for WebSocketResource functionality."""

from __future__ import annotations

import typing

import msgspec
import msgspec.json as msgspec_json
import pytest

from falcon_pachinko import WebSocketLike, WebSocketResource
from falcon_pachinko.unittests.helpers import DummyWS


class EchoPayload(msgspec.Struct):
    """A simple message payload structure for testing echo messages."""

    text: str


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
        self.seen: list[typing.Any] = []
        self.fallback: list[typing.Any] = []

    async def on_message(self, ws: WebSocketLike, message: str | bytes) -> None:
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
        self.received: list[typing.Any] = []

    async def on_message(self, ws: WebSocketLike, message: str | bytes) -> None:
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
    payload : typing.Any
        The raw payload received with the message. Can be any type, including
        None
    """
    self.received.append(payload)


RawResource.add_handler("raw", raw_handler, payload_type=None)


class ConventionalResource(WebSocketResource):
    """Resource used to test ``on_{tag}`` dispatch."""

    def __init__(self) -> None:
        self.seen: list[typing.Any] = []

    async def on_echo(self, ws: WebSocketLike, payload: object) -> None:
        """Record ``payload`` from ``echo`` messages."""
        self.seen.append(payload)


class CamelResource(WebSocketResource):
    """Resource testing CamelCase tag conversion."""

    class SendMessage(msgspec.Struct, tag="sendMessage"):
        """Payload for a send message."""

        text: str

    schema = SendMessage

    def __init__(self) -> None:
        self.messages: list[str] = []

    async def on_send_message(self, ws: WebSocketLike, payload: SendMessage) -> None:
        """Record ``payload`` text from ``sendMessage`` messages."""
        self.messages.append(payload.text)


@pytest.mark.asyncio
async def test_dispatch_calls_registered_handler() -> None:
    """Test that dispatching a message with a registered type calls the handler."""
    r = EchoResource()
    raw = msgspec_json.encode({"type": "echo", "payload": {"text": "hi"}})
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
    raw = msgspec_json.encode({"type": "unknown", "payload": {"text": "oops"}})
    await r.dispatch(DummyWS(), raw)
    assert r.fallback == [raw]


@pytest.mark.asyncio
async def test_handler_shared_across_instances() -> None:
    """Test that handlers are shared across instances of the same resource class."""
    r1 = EchoResource()
    r2 = EchoResource()
    raw = msgspec_json.encode({"type": "echo", "payload": {"text": "hey"}})
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
    msg: dict[str, typing.Any] = {"type": "raw"}
    if payload != "MISSING":
        msg["payload"] = payload
    raw = msgspec_json.encode(msg)
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
    raw = msgspec_json.encode({"type": "echo", "payload": {"text": 42}})
    await r.dispatch(DummyWS(), raw)
    assert r.fallback == [raw]
    assert not r.seen


@pytest.mark.asyncio
async def test_on_tag_dispatch_envelope() -> None:
    """Messages with matching ``on_{tag}`` handlers are dispatched."""
    r = ConventionalResource()
    raw = msgspec_json.encode({"type": "echo", "payload": {"x": 1}})
    await r.dispatch(DummyWS(), raw)
    assert r.seen == [{"x": 1}]


@pytest.mark.asyncio
async def test_on_tag_camel_case() -> None:
    """CamelCase tags are converted to snake_case."""
    r = CamelResource()
    raw = msgspec_json.encode(CamelResource.SendMessage(text="hi"))
    await r.dispatch(DummyWS(), raw)
    assert r.messages == ["hi"]
