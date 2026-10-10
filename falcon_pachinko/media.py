"""Raw WebSocket frame handlers for Falcon's ASGI media layer."""

from __future__ import annotations

import typing as typ

import falcon
import falcon.asgi
import falcon.media
import msgspec.json as msjson
import msgspec.msgpack as msmsgpack

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


def configure_raw_frame_media(app: falcon.asgi.App) -> None:
    """Configure an ASGI app to pass raw WebSocket frames to the router.

    Falcon normally decodes media before returning it from
    ``WebSocket.receive_media()``. This helper preserves TEXT and BINARY
    payloads so the router can pass them unchanged to resource dispatch.
    """
    app.ws_options.media_handlers[falcon.WebSocketPayloadType.TEXT] = (
        RawTextMediaHandler()
    )
    app.ws_options.media_handlers[falcon.WebSocketPayloadType.BINARY] = (
        RawBinaryMediaHandler()
    )
