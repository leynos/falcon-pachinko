"""Tests for schema-driven dispatch using msgspec tagged unions."""

import typing

import msgspec
import msgspec.json as msgspec_json
import pytest

from falcon_pachinko import WebSocketLike, WebSocketResource, handles_message
from falcon_pachinko.unittests.helpers import DummyWS


class Join(msgspec.Struct, tag="join"):
    """Message structure for join events."""

    room: str


class Leave(msgspec.Struct, tag="leave"):
    """Message structure for leave events."""

    room: str


MessageUnion = Join | Leave


class SchemaResource(WebSocketResource):
    """Resource using a schema for automatic message dispatch."""

    schema = MessageUnion

    def __init__(self) -> None:
        """Initialize with an empty events list."""
        self.events: list[tuple[str, typing.Any]] = []

    @handles_message("join")
    async def handle_join(self, ws: WebSocketLike, payload: Join) -> None:
        """Record join events."""
        self.events.append(("join", payload.room))

    @handles_message("leave")
    async def handle_leave(self, ws: WebSocketLike, payload: Leave) -> None:
        """Record leave events."""
        self.events.append(("leave", payload.room))

    async def on_message(self, ws: WebSocketLike, message: str | bytes) -> None:
        """Record fallback messages."""
        self.events.append(("raw", message))


@pytest.mark.asyncio
async def test_schema_dispatch_to_handlers() -> None:
    """Messages matching the schema are routed to decorated handlers."""
    r = SchemaResource()
    await r.dispatch(DummyWS(), msgspec_json.encode(Join(room="a")))
    await r.dispatch(DummyWS(), msgspec_json.encode(Leave(room="b")))
    assert r.events == [("join", "a"), ("leave", "b")]


@pytest.mark.asyncio
async def test_schema_unknown_tag_calls_fallback() -> None:
    """Unknown tags invoke the fallback handler with the raw message."""
    r = SchemaResource()
    raw = msgspec_json.encode({"type": "oops", "room": "x"})
    await r.dispatch(DummyWS(), raw)
    assert r.events == [("raw", raw)]


@pytest.mark.asyncio
async def test_schema_decode_error_calls_fallback() -> None:
    """Decode failures also trigger the fallback handler."""
    r = SchemaResource()
    await r.dispatch(DummyWS(), b"not json")
    assert r.events == [("raw", b"not json")]
