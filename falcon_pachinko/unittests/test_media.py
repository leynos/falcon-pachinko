"""Tests for Falcon's opt-in raw WebSocket media handlers."""

from __future__ import annotations

import dataclasses as dc
import typing as typ

import falcon
import falcon.asgi
import falcon.media
import msgspec.json as msjson
import msgspec.msgpack as msmsgpack
import pytest

from falcon_pachinko.media import (
    RawBinaryMediaHandler,
    RawTextMediaHandler,
    configure_raw_frame_media,
)


def test_raw_text_handler_preserves_received_text_and_encodes_json() -> None:
    """TEXT receive stays raw while outbound media uses msgspec JSON."""
    handler = RawTextMediaHandler()
    payload = '{"type":"ping","payload":"hello"}'

    assert handler.deserialize(payload) == payload, (
        "the TEXT media handler must preserve the incoming frame"
    )
    assert handler.serialize({"type": "reply"}) == msjson.encode({
        "type": "reply"
    }).decode("utf-8"), "outgoing TEXT media must be JSON encoded"


def test_raw_binary_handler_preserves_bytes_and_encodes_msgpack() -> None:
    """BINARY receive stays raw and outbound media uses MessagePack."""
    handler = RawBinaryMediaHandler()
    payload = b"\x00\x01"

    assert handler.deserialize(payload) is payload, (
        "the BINARY media handler must preserve the received bytes"
    )
    assert handler.serialize(payload) == payload, (
        "outgoing bytes must pass through without MessagePack encoding"
    )
    assert handler.serialize({"type": "reply"}) == msmsgpack.encode({
        "type": "reply"
    }), "other BINARY media must use MessagePack encoding"


def test_raw_frame_handlers_are_installed_for_both_payload_types() -> None:
    """Configuration preserves incoming frames and the existing TEXT encoder."""
    app = falcon.asgi.App()
    media = {"reply": "hello"}
    original_text = app.ws_options.media_handlers[falcon.WebSocketPayloadType.TEXT]
    expected_text = original_text.serialize(media)

    configure_raw_frame_media(app)

    handlers = app.ws_options.media_handlers
    text = typ.cast(
        "falcon.media.TextBaseHandlerWS", handlers[falcon.WebSocketPayloadType.TEXT]
    )
    binary = typ.cast(
        "falcon.media.BinaryBaseHandlerWS", handlers[falcon.WebSocketPayloadType.BINARY]
    )
    raw_text, raw_binary = '"quoted text"', b"\xffnot-json"
    assert text.deserialize(raw_text) is raw_text, "TEXT input must remain unmodified"
    assert binary.deserialize(raw_binary) is raw_binary, (
        "BINARY input must remain unmodified"
    )
    assert text.serialize(media) == expected_text, (
        "TEXT serialization must remain unchanged"
    )


@dc.dataclass(slots=True)
class _TextHandler(falcon.media.TextBaseHandlerWS):
    """Expose serializer state and reject unexpected deserializer probes."""

    calls: list[object] = dc.field(default_factory=list)

    @typ.override
    def serialize(self, media: object) -> str:
        """Record the original media object on this handler instance."""
        self.calls.append(media)
        return "custom-text"

    @typ.override
    def deserialize(self, payload: str) -> object:
        """Reject configuration code that tries to deserialize a sample."""
        pytest.fail("configuration must not probe the original TEXT deserializer")


@dc.dataclass(slots=True)
class _BinaryHandler(falcon.media.BinaryBaseHandlerWS):
    """Return a caller-owned buffer without changing its identity."""

    output: bytes | bytearray | memoryview
    calls: list[object] = dc.field(default_factory=list)

    @typ.override
    def serialize(self, media: object) -> bytes | bytearray | memoryview:
        """Record the media object and preserve the configured buffer."""
        self.calls.append(media)
        return self.output

    @typ.override
    def deserialize(self, payload: bytes) -> object:
        """Reject configuration code that tries to deserialize a sample."""
        pytest.fail("configuration must not probe the original BINARY deserializer")


@pytest.mark.parametrize(
    "output",
    [b"custom-binary", bytearray(b"custom-binary"), memoryview(b"custom-binary")],
    ids=["bytes", "bytearray", "memoryview"],
)
def test_configuration_preserves_original_serializers_without_probes(
    output: bytes | bytearray | memoryview,
) -> None:
    """Setup never probes callbacks and serialization retains handler state."""
    app = falcon.asgi.App()
    text, binary = _TextHandler(), _BinaryHandler(output)
    app.ws_options.media_handlers = {
        falcon.WebSocketPayloadType.TEXT: text,
        falcon.WebSocketPayloadType.BINARY: binary,
    }

    configure_raw_frame_media(app)

    assert not text.calls, "setup must not invoke the TEXT serializer"
    assert not binary.calls, "setup must not invoke the BINARY serializer"
    media = {"reply": "input"}
    handlers = app.ws_options.media_handlers
    assert handlers[falcon.WebSocketPayloadType.BINARY].serialize(media) is output, (
        "the original BINARY serializer's buffer must be returned unchanged"
    )
    assert (
        handlers[falcon.WebSocketPayloadType.TEXT].serialize(media) == "custom-text"
    ), "the original custom TEXT serializer must remain active"
    assert len(text.calls) == 1, "the original TEXT serializer must run exactly once"
    assert len(binary.calls) == 1, (
        "the original BINARY serializer must run exactly once"
    )
    assert text.calls[0] is media, (
        "the original TEXT handler must receive the same object"
    )
    assert binary.calls[0] is media, (
        "the original BINARY handler must receive the same object"
    )


def test_configuration_is_idempotent_and_affects_only_the_selected_app() -> None:
    """Repeated setup keeps wrappers stable and leaves other apps untouched."""
    app, untouched = falcon.asgi.App(), falcon.asgi.App()
    other_handlers = dict(untouched.ws_options.media_handlers)
    configure_raw_frame_media(app)
    first_handlers = dict(app.ws_options.media_handlers)

    configure_raw_frame_media(app)

    assert all(
        app.ws_options.media_handlers[key] is handler
        for key, handler in first_handlers.items()
    ), "repeated configuration must retain installed handler identities"
    assert all(
        untouched.ws_options.media_handlers[key] is handler
        for key, handler in other_handlers.items()
    ), "configuration must not change another application's handlers"
