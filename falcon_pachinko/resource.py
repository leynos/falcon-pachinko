from __future__ import annotations

import collections.abc as cabc
import typing

import msgspec


class _Envelope(msgspec.Struct, frozen=True):
    type: str
    payload: typing.Any | None = None


Handler = cabc.Callable[[typing.Any, typing.Any, typing.Any], cabc.Awaitable[None]]


class WebSocketResource:
    """Base class for WebSocket handlers."""

    handlers: typing.ClassVar[dict[str, tuple[Handler, type | None]]]

    def __init_subclass__(cls, **kwargs: typing.Any) -> None:
        super().__init_subclass__(**kwargs)
        cls.handlers = {}

    async def on_connect(
        self, req: typing.Any, ws: typing.Any, **params: typing.Any
    ) -> bool:
        """Called when the WebSocket handshake is complete."""
        return True

    async def on_disconnect(self, ws: typing.Any, close_code: int) -> None:
        """Called when the WebSocket disconnects."""

    async def on_message(self, ws: typing.Any, message: str | bytes) -> None:
        """Fallback for unhandled messages."""

    @classmethod
    def add_handler(
        cls, message_type: str, handler: Handler, *, payload_type: type | None = None
    ) -> None:
        """Register ``handler`` for ``message_type``."""
        cls.handlers[message_type] = (handler, payload_type)

    async def dispatch(self, ws: typing.Any, raw: str | bytes) -> None:
        """Dispatch a raw WebSocket message to a handler."""
        try:
            envelope = msgspec.json.decode(raw, type=_Envelope)
        except msgspec.DecodeError:
            await self.on_message(ws, raw)
            return

        entry = self.__class__.handlers.get(envelope.type)
        if not entry:
            await self.on_message(ws, raw)
            return

        handler, payload_type = entry
        payload: typing.Any = envelope.payload
        if payload_type is not None and payload is not None:
            try:
                payload = typing.cast(
                    "typing.Any",
                    msgspec.convert(payload, type=payload_type),
                )
            except (msgspec.ValidationError, TypeError):
                await self.on_message(ws, raw)
                return
        await handler(self, ws, payload)
