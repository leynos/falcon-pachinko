from __future__ import annotations

import typing

import msgspec
import pytest

from falcon_pachinko.resource import WebSocketResource


class EchoPayload(msgspec.Struct):
    text: str


class DummyWS:
    pass


class EchoResource(WebSocketResource):
    def __init__(self) -> None:
        self.seen: list[typing.Any] = []
        self.fallback: list[typing.Any] = []

    async def on_message(self, ws: typing.Any, message: str | bytes) -> None:
        self.fallback.append(message)


async def echo_handler(
    self: EchoResource, ws: typing.Any, payload: EchoPayload
) -> None:
    self.seen.append(payload.text)


EchoResource.add_handler("echo", echo_handler, payload_type=EchoPayload)


@pytest.mark.asyncio()
async def test_dispatch_calls_registered_handler() -> None:
    r = EchoResource()
    raw = msgspec.json.encode({"type": "echo", "payload": {"text": "hi"}})
    await r.dispatch(DummyWS(), raw)
    assert r.seen == ["hi"]
    assert not r.fallback


@pytest.mark.asyncio()
async def test_dispatch_unknown_type_calls_fallback() -> None:
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
