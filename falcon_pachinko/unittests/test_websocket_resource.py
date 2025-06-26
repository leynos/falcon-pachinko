from __future__ import annotations

import typing

import msgspec
import pytest

from falcon_pachinko.resource import WebSocketLike, WebSocketResource


class EchoPayload(msgspec.Struct):
    text: str


class DummyWS:
    async def accept(self, subprotocol: str | None = None) -> None:
        pass

    async def close(self, code: int = 1000) -> None:
        pass

    async def send_media(self, data: typing.Any) -> None:
        pass


class EchoResource(WebSocketResource):
    def __init__(self) -> None:
        """
        Initializes the EchoResource with empty lists for handled and fallback messages.

        The `seen` list stores texts from successfully handled payloads, while the `fallback` list records messages that do not match any registered handler or fail payload validation.
        """
        self.seen: list[typing.Any] = []
        self.fallback: list[typing.Any] = []

    async def on_message(self, ws: WebSocketLike, message: str | bytes) -> None:
        """
        Handles messages that do not match any registered handler by appending them to the fallback list.

        Args:
            ws: The WebSocket connection instance.
            message: The raw message received, as a string or bytes.
        """
        self.fallback.append(message)


async def echo_handler(
    self: EchoResource, ws: WebSocketLike, payload: EchoPayload
) -> None:
    """
    Handles an "echo" message by recording the payload text.

    Appends the `text` field from the received `EchoPayload` to the resource's `seen` list.
    """
    self.seen.append(payload.text)


EchoResource.add_handler("echo", echo_handler, payload_type=EchoPayload)


class RawResource(WebSocketResource):
    def __init__(self) -> None:
        """
        Initializes the RawResource instance with an empty list to store received messages or payloads.
        """
        self.received: list[typing.Any] = []

    async def on_message(self, ws: WebSocketLike, message: str | bytes) -> None:
        """
        Handles incoming messages by appending them to the received list.

        This method acts as a fallback for messages that do not match any registered handler.
        """
        self.received.append(message)


async def raw_handler(
    self: RawResource, ws: WebSocketLike, payload: typing.Any
) -> None:
    """
    Handles incoming messages of type "raw" by appending the payload to the resource's received list.

    Args:
        payload: The raw payload received with the message. Can be any type, including None.
    """
    self.received.append(payload)


RawResource.add_handler("raw", raw_handler, payload_type=None)


@pytest.mark.asyncio()
async def test_dispatch_calls_registered_handler() -> None:
    r = EchoResource()
    raw = msgspec.json.encode({"type": "echo", "payload": {"text": "hi"}})
    await r.dispatch(DummyWS(), raw)
    assert r.seen == ["hi"]
    assert not r.fallback


@pytest.mark.asyncio()
async def test_dispatch_unknown_type_calls_fallback() -> None:
    """
    Tests that dispatching a message with an unknown type invokes the fallback handler.

    Verifies that when a message with an unregistered type is dispatched to EchoResource, the raw message is appended to the resource's fallback list.
    """
    r = EchoResource()
    raw = msgspec.json.encode({"type": "unknown", "payload": {"text": "oops"}})
    await r.dispatch(DummyWS(), raw)
    assert r.fallback == [raw]


@pytest.mark.asyncio()
async def test_handler_shared_across_instances() -> None:
    r1 = EchoResource()
    r2 = EchoResource()
    raw = msgspec.json.encode({"type": "echo", "payload": {"text": "hey"}})
    await r1.dispatch(DummyWS(), raw)
    await r2.dispatch(DummyWS(), raw)
    assert r1.seen == ["hey"]
    assert r2.seen == ["hey"]


@pytest.mark.asyncio()
@pytest.mark.parametrize(
    "payload,expected",
    [
        ({"text": "hi"}, {"text": "hi"}),
        (None, None),
        ("MISSING", None),
    ],
)
async def test_payload_type_none_passes_raw(
    payload: typing.Any, expected: typing.Any
) -> None:
    """
    Tests that RawResource receives the raw payload as-is when no payload type is specified.

    Verifies that the received list contains the exact payload passed, or None if the payload is missing.
    """
    r = RawResource()
    msg: dict[str, typing.Any] = {"type": "raw"}
    if payload != "MISSING":
        msg["payload"] = payload
    raw = msgspec.json.encode(msg)
    await r.dispatch(DummyWS(), raw)
    assert r.received == [expected]


@pytest.mark.asyncio()
async def test_invalid_payload_calls_fallback() -> None:
    """
    Tests that an invalid payload type causes the message to be handled by the fallback method.

    Sends a message with an incorrect payload type to EchoResource and verifies that it is appended to the fallback list and not processed by the registered handler.
    """
    r = EchoResource()
    raw = msgspec.json.encode({"type": "echo", "payload": {"text": 42}})
    await r.dispatch(DummyWS(), raw)
    assert r.fallback == [raw]
    assert not r.seen
