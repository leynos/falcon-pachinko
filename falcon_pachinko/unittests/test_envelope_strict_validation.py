"""Strict unknown-field validation for envelope-mode dispatch.

Envelope dispatch validates the decoded payload mapping before converting it,
so renamed fields must arrive under their encoded wire names. Schema mode has
mirror-image coverage in ``test_schema_strict_validation``.
"""

import msgspec as ms
import msgspec.json as msjson
import pytest

from falcon_pachinko import WebSocketLike, WebSocketResource, handles_message
from falcon_pachinko.unittests.helpers import DummyWS


class RenamedPayload(ms.Struct, rename="camel"):
    """Payload whose wire field name differs from its attribute name."""

    room_name: str


class RenamedStrictResource(WebSocketResource):
    """Resource validating a renamed payload strictly."""

    def __init__(self) -> None:
        """Start with no recorded payloads or rejected frames."""
        self.seen: list[str] = []
        self.fallback: list[str | bytes] = []

    async def on_unhandled(self, ws: WebSocketLike, message: str | bytes) -> None:
        """Record messages that fail validation."""
        self.fallback.append(message)

    @handles_message("renamed")
    async def handle_renamed(self, ws: WebSocketLike, payload: RenamedPayload) -> None:
        """Record the room under its Python attribute name."""
        self.seen.append(payload.room_name)


@pytest.mark.asyncio
async def test_extra_fields_strict_accepts_encoded_field_name() -> None:
    """Strict validation accepts the encoded name declared by ``rename``."""
    r = RenamedStrictResource()
    r.bind_default_hook_manager()
    raw = msjson.encode({"type": "renamed", "payload": {"roomName": "lobby"}})
    await r.dispatch(DummyWS(), raw)
    assert r.seen == ["lobby"], "the encoded wire name must satisfy strict validation"
    assert not r.fallback, "the encoded wire name must not fall back"


@pytest.mark.asyncio
async def test_extra_fields_strict_rejects_attribute_name() -> None:
    """The Python attribute name is not a valid wire name."""
    r = RenamedStrictResource()
    r.bind_default_hook_manager()
    raw = msjson.encode({"type": "renamed", "payload": {"room_name": "lobby"}})
    await r.dispatch(DummyWS(), raw)
    assert r.fallback == [raw], "the Python attribute name must be rejected on the wire"
    assert not r.seen, "the handler must not run for an invalid wire name"
