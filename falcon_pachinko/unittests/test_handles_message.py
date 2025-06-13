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


def test_missing_payload_param_raises() -> None:
    with pytest.raises(TypeError):

        class BadSig(WebSocketResource):  # pyright: ignore[reportUnusedClass]
            @handles_message("oops")  # pyright: ignore[reportArgumentType]
            async def bad(self, ws: typing.Any) -> None: ...


class ParentResource(WebSocketResource):
    @handles_message("parent")
    async def parent(self, ws: typing.Any, payload: typing.Any) -> None: ...


class ChildResource(ParentResource):
    def __init__(self) -> None:
        self.invoked: list[str] = []

    @handles_message("child")
    async def child(self, ws: typing.Any, payload: typing.Any) -> None:
        self.invoked.append("child")

    async def parent(self, ws: typing.Any, payload: typing.Any) -> None:  # pyright: ignore[reportIncompatibleVariableOverride]
        # override to record
        self.invoked.append("parent")


@pytest.mark.asyncio()
async def test_handlers_inherited() -> None:
    r = ChildResource()
    await r.dispatch(DummyWS(), msgspec.json.encode({"type": "parent"}))
    await r.dispatch(DummyWS(), msgspec.json.encode({"type": "child"}))
    assert r.invoked == ["parent", "child"]
