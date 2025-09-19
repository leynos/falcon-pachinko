"""Tests for schema-driven dispatch using msgspec tagged unions."""

import typing as typ

import msgspec as ms
import msgspec.json as msjson
import pytest

from falcon_pachinko import WebSocketLike, WebSocketResource, handles_message
from falcon_pachinko.unittests.helpers import DummyWS, bind_default_hooks


class Join(ms.Struct, tag="join"):
    """Message structure for join events."""

    room: str


class Leave(ms.Struct, tag="leave"):
    """Message structure for leave events."""

    room: str


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


@pytest.mark.asyncio
async def test_schema_dispatch_to_handlers() -> None:
    """Messages matching the schema are routed to decorated handlers."""
    r = SchemaResource()
    bind_default_hooks(r)
    await r.dispatch(DummyWS(), msjson.encode(Join(room="a")))
    await r.dispatch(DummyWS(), msjson.encode(Leave(room="b")))
    assert r.events == [("join", "a"), ("leave", "b")]


@pytest.mark.asyncio
async def test_schema_unknown_tag_calls_fallback() -> None:
    """Unknown tags invoke the fallback handler with the raw message."""
    r = SchemaResource()
    bind_default_hooks(r)
    raw = msjson.encode({"type": "oops", "room": "x"})
    await r.dispatch(DummyWS(), raw)
    assert r.events == [("raw", raw)]


@pytest.mark.asyncio
async def test_schema_decode_error_calls_fallback() -> None:
    """Decode failures also trigger the fallback handler."""
    r = SchemaResource()
    bind_default_hooks(r)
    await r.dispatch(DummyWS(), b"not json")
    assert r.events == [("raw", b"not json")]


def test_invalid_schema_type_raises() -> None:
    """Only tagged msgspec.Struct types are allowed in ``schema``."""

    class Good(ms.Struct, tag="good"):
        pass

    class Bad:
        pass

    with pytest.raises(TypeError):

        class BadResource(WebSocketResource):
            schema = Good | Bad


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

    assert "Payload" in str(exc.value)
    assert "BadResource.h2" in str(exc.value)
