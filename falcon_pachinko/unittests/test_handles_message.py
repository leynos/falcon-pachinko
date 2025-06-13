from __future__ import annotations

import typing

import msgspec
import pytest

from falcon_pachinko import WebSocketResource, handles_message


class DummyWS:
    pass


class PingPayload(msgspec.Struct):
    text: str


class DecoratedResource(WebSocketResource):
    def __init__(self) -> None:
        self.seen: list[str] = []

    @handles_message("ping")
    async def handle_ping(self, ws: typing.Any, payload: PingPayload) -> None:
        self.seen.append(payload.text)


@pytest.mark.asyncio()
async def test_decorator_registers_handler() -> None:
    r = DecoratedResource()
    raw = msgspec.json.encode({"type": "ping", "payload": {"text": "hi"}})
    await r.dispatch(DummyWS(), raw)
    assert r.seen == ["hi"]


def test_duplicate_handler_raises() -> None:
    with pytest.raises(RuntimeError):

        class BadResource(WebSocketResource):  # pyright: ignore[reportUnusedClass]
            @handles_message("dup")
            async def h1(self, ws: typing.Any, payload: typing.Any) -> None: ...

            @handles_message("dup")
            async def h2(self, ws: typing.Any, payload: typing.Any) -> None: ...
