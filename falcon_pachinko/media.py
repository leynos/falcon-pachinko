"""Raw WebSocket frame handlers for Falcon's ASGI media layer."""

from __future__ import annotations

import typing as typ

import falcon
import falcon.asgi
import falcon.media
import msgspec.json as msjson
import msgspec.msgpack as msmsgpack

if typ.TYPE_CHECKING:
    import collections.abc as cabc

__all__ = ["RawBinaryMediaHandler", "RawTextMediaHandler", "configure_raw_frame_media"]


class RawTextMediaHandler(falcon.media.TextBaseHandlerWS):
    """Preserve incoming TEXT frames and encode outgoing media as JSON."""

    @typ.override
    def serialize(self, media: object) -> str:
        """Encode ``media`` as UTF-8 JSON text."""
        return msjson.encode(media).decode("utf-8")

    @typ.override
    def deserialize(self, payload: str) -> object:
        """Return the unmodified TEXT frame for schema-aware dispatch."""
        return payload


class RawBinaryMediaHandler(falcon.media.BinaryBaseHandlerWS):
    """Preserve incoming BINARY frames and encode other media as MessagePack."""

    @typ.override
    def serialize(self, media: object) -> bytes:
        """Pass bytes-like values through and MessagePack-encode other media."""
        if isinstance(media, bytes | bytearray | memoryview):
            return bytes(media)
        return msmsgpack.encode(media)

    @typ.override
    def deserialize(self, payload: bytes) -> object:
        """Return the unmodified BINARY frame for schema-aware dispatch."""
        return payload


class _RawHandler[SerializedT]:
    """Retain an existing serializer without invoking application code."""

    def __init__(self, serialize: cabc.Callable[[object], SerializedT]) -> None:
        """Retain the bound callback without probing application code."""
        self.serialize: cabc.Callable[[object], SerializedT] = serialize


class _RawTextHandler(_RawHandler[str], falcon.media.TextBaseHandlerWS):
    """Provide Falcon's nominal TEXT interface for the preserving adapter."""

    @typ.override
    def deserialize(self, payload: str) -> str:
        """Return the exact incoming TEXT frame without decoding or copying."""
        return payload


class _RawBinaryHandler(
    _RawHandler[bytes | bytearray | memoryview], falcon.media.BinaryBaseHandlerWS
):
    """Provide Falcon's nominal BINARY interface without narrowing buffers."""

    @typ.override
    def deserialize(self, payload: bytes) -> bytes:
        """Return the exact incoming BINARY frame without decoding or copying."""
        return payload


def configure_raw_frame_media(app: falcon.asgi.App) -> None:
    """Configure an ASGI app to pass raw WebSocket frames to the router.

    Falcon normally decodes media before returning it from
    ``WebSocket.receive_media()``. This helper preserves TEXT and BINARY
    payloads so the router can pass them unchanged to resource dispatch, while
    delegating outgoing serialization to the app's existing handlers.

    Configure the app before Falcon constructs any WebSocket. The configuration
    affects every WebSocket route on this app, including routes outside the
    Pachinko router. Repeated setup retains the installed adapters. Neither
    configuration nor importing this module invokes serializer callbacks.

    Parameters
    ----------
    app : falcon.asgi.App
        Application whose WebSocket media handlers should preserve raw input.
    """
    handlers = app.ws_options.media_handlers
    # Falcon's enum-keyed mapping pairs TEXT with TextBaseHandlerWS and BINARY
    # with BinaryBaseHandlerWS; its union annotation cannot retain that pairing.
    text = typ.cast(
        "falcon.media.TextBaseHandlerWS", handlers[falcon.WebSocketPayloadType.TEXT]
    )
    binary = typ.cast(
        "falcon.media.BinaryBaseHandlerWS", handlers[falcon.WebSocketPayloadType.BINARY]
    )
    if not isinstance(text, _RawTextHandler):
        handlers[falcon.WebSocketPayloadType.TEXT] = _RawTextHandler(text.serialize)
    if not isinstance(binary, _RawBinaryHandler):
        handlers[falcon.WebSocketPayloadType.BINARY] = _RawBinaryHandler(
            binary.serialize
        )
