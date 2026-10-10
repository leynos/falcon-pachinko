"""Pass raw WebSocket frames through Falcon's media handler boundary."""

from __future__ import annotations

import functools
import types
import typing as typ
import warnings

import falcon
import falcon.asgi

if typ.TYPE_CHECKING:
    import collections.abc as cabc

_DEFAULT_WEBSOCKET_MEDIA_HANDLERS = falcon.asgi.WebSocketOptions().media_handlers


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


def _configuration_value(value: object) -> object:
    """Normalize mutable handler configuration for a default comparison."""
    match value:
        case functools.partial() as partial:
            return (
                partial.func,
                partial.args,
                _configuration_value(partial.keywords or {}),
            )
        case types.MethodType() as method:
            return method.__func__
        case types.BuiltinMethodType() as method:
            return (method.__name__, type(method.__self__))
        case dict() as mapping:
            return tuple(
                sorted(
                    (key, _configuration_value(item)) for key, item in mapping.items()
                )
            )
        case list() | tuple() as sequence:
            return tuple(_configuration_value(item) for item in sequence)
        case _:
            return value


def _matches_default_handler(handler: object, default_handler: object) -> bool:
    """Return whether ``handler`` has Falcon's default class and settings."""
    if type(handler) is not type(default_handler):
        return False
    handler_state = _configuration_value(getattr(handler, "__dict__", {}))
    default_state = _configuration_value(getattr(default_handler, "__dict__", {}))
    return handler_state == default_state


def install_raw_websocket_media_handlers(
    media_handlers: cabc.MutableMapping[falcon.WebSocketPayloadType, object],
) -> None:
    """Wrap app-wide text and binary handlers once for raw frame dispatch.

    Falcon shares ``ws_options.media_handlers`` across all WebSocket routes in
    an application. Warn if those handlers have been customized because this
    wrapper replaces their inbound deserializer with pass-through behaviour.
    """
    payload_types = (
        falcon.WebSocketPayloadType.TEXT,
        falcon.WebSocketPayloadType.BINARY,
    )
    custom_handlers = [
        payload_type.name
        for payload_type in payload_types
        if not isinstance(
            (handler := media_handlers[payload_type]), _RawWebSocketMediaHandler
        )
        and not _matches_default_handler(
            handler, _DEFAULT_WEBSOCKET_MEDIA_HANDLERS[payload_type]
        )
    ]
    if custom_handlers:
        names = ", ".join(custom_handlers)
        warnings.warn(
            "attach() replaces customized app-wide WebSocket media handlers "
            f"for {names}; all Falcon WebSocket responders on this app will "
            "receive raw str or bytes from receive_media()",
            UserWarning,
            stacklevel=3,
        )

    for payload_type in payload_types:
        handler = media_handlers[payload_type]
        if not isinstance(handler, _RawWebSocketMediaHandler):
            media_handlers[payload_type] = _RawWebSocketMediaHandler(handler)
