"""Pass raw WebSocket frames through Falcon's media handler boundary."""

from __future__ import annotations

import typing as typ

import falcon

if typ.TYPE_CHECKING:
    import collections.abc as cabc


class _RawWebSocketMediaHandler:
    """Preserve inbound frames while retaining Falcon's outbound serializer."""

    def __init__(self, wrapped: object) -> None:
        serialize = getattr(wrapped, "serialize", None)
        if not callable(serialize):
            msg = "Falcon WebSocket media handler must provide serialize()"
            raise TypeError(msg)
        self._serialize = typ.cast("cabc.Callable[[object], object]", serialize)

    @staticmethod
    def deserialize(payload: object) -> object:
        """Return the exact text or binary frame received from the peer."""
        return payload

    # pylint: disable-next=trivial-attribute-wrapper  # Keep Falcon's serializer intact.
    def serialize(self, media: object) -> object:
        """Delegate outbound media serialization to Falcon's configured handler."""
        return self._serialize(media)


def install_raw_websocket_media_handlers(
    media_handlers: cabc.MutableMapping[falcon.WebSocketPayloadType, object],
) -> None:
    """Wrap Falcon's text and binary handlers once for raw frame dispatch."""
    for payload_type in (
        falcon.WebSocketPayloadType.TEXT,
        falcon.WebSocketPayloadType.BINARY,
    ):
        handler = media_handlers[payload_type]
        if not isinstance(handler, _RawWebSocketMediaHandler):
            media_handlers[payload_type] = _RawWebSocketMediaHandler(handler)
