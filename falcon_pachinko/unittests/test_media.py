"""Tests for Falcon's opt-in raw WebSocket media handlers."""

from __future__ import annotations

import falcon
import falcon.asgi
import msgspec.json as msjson
import msgspec.msgpack as msmsgpack

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
    """The public helper configures TEXT and BINARY on Falcon's app."""
    app = falcon.asgi.App()

    configure_raw_frame_media(app)

    handlers = app.ws_options.media_handlers
    assert isinstance(
        handlers[falcon.WebSocketPayloadType.TEXT], RawTextMediaHandler
    ), "the app must use raw text handling"
    assert isinstance(
        handlers[falcon.WebSocketPayloadType.BINARY], RawBinaryMediaHandler
    ), "the app must use raw binary handling"
