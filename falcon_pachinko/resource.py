from __future__ import annotations

import collections.abc as cabc
import functools
import inspect
import typing

import msgspec


class _Envelope(msgspec.Struct, frozen=True):
    type: str
    payload: typing.Any | None = None


Handler = cabc.Callable[[typing.Any, typing.Any, typing.Any], cabc.Awaitable[None]]


class _HandlesMessageDescriptor:
    """Register a method as a message handler on its class."""

    def __init__(self, message_type: str, func: Handler) -> None:
        self.message_type = message_type
        self.func = func
        functools.update_wrapper(self, func)  # pyright: ignore[reportArgumentType]
        self.owner: type | None = None
        self.name: str | None = None

    def __set_name__(self, owner: type, name: str) -> None:
        self.owner = owner
        self.name = name

        typed_owner = typing.cast("type[WebSocketResource]", owner)
        if not hasattr(typed_owner, "handlers"):
            typed_owner.handlers = {}

        if self.message_type in typed_owner.handlers:
            msg = (
                f"Duplicate handler for message type {self.message_type!r} "
                f"on {owner.__qualname__}"
            )
            raise RuntimeError(msg)

        payload_type: type | None = None
        try:
            sig = inspect.signature(self.func)
            params = list(sig.parameters.values())
            if len(params) >= 3:
                annotation = params[2].annotation
                if annotation is not inspect.Signature.empty:
                    payload_type = typing.cast("type | None", annotation)
        except (ValueError, TypeError):
            payload_type = None

        typed_owner.add_handler(self.message_type, self.func, payload_type=payload_type)

    def __get__(self, instance: typing.Any, owner: type | None = None) -> Handler:
        return self.func.__get__(instance, owner or self.owner)


def handles_message(
    message_type: str,
) -> cabc.Callable[[Handler], _HandlesMessageDescriptor]:
    """Decorator factory to mark a method as a WebSocket message handler."""

    def decorator(func: Handler) -> _HandlesMessageDescriptor:
        return _HandlesMessageDescriptor(message_type, func)

    return decorator


class WebSocketResource:
    """Base class for WebSocket handlers."""

    handlers: typing.ClassVar[dict[str, tuple[Handler, type | None]]]

    def __init_subclass__(cls, **kwargs: typing.Any) -> None:
        """
        Initializes the handlers dictionary for each subclass of
        WebSocketResource.

        Ensures that each subclass has its own independent mapping of message
        types to handler functions.
        """
        super().__init_subclass__(**kwargs)
        cls.handlers = {}

    async def on_connect(
        self, req: typing.Any, ws: typing.Any, **params: typing.Any
    ) -> bool:
        """
        Called after the WebSocket handshake is complete to decide whether the
        connection should be accepted.

        Args:
            req: The incoming HTTP request associated with the WebSocket handshake.
            ws: The WebSocket connection object.
            **params: Additional parameters relevant to the connection.

        Returns:
            True to accept the WebSocket connection; False to reject it.
        """
        return True

    async def on_disconnect(self, ws: typing.Any, close_code: int) -> None:
        """
        Handles cleanup or custom logic when the WebSocket connection is closed.

        Args:
            ws: The WebSocket connection instance.
            close_code: The close code indicating the reason for disconnection.
        """

    async def on_message(self, ws: typing.Any, message: str | bytes) -> None:
        """
        Handles incoming WebSocket messages that do not match any registered handler.

        Called when a message cannot be decoded or its type is unrecognized.
        Override to implement custom fallback behavior for such messages.
        """

    @classmethod
    def add_handler(
        cls, message_type: str, handler: Handler, *, payload_type: type | None = None
    ) -> None:
        """
        Registers a handler function for a specific message type.

        Associates the given handler with the specified message type.
        Optionally, a payload type can be provided for automatic payload
        validation and conversion.
        """
        cls.handlers[message_type] = (handler, payload_type)

    async def dispatch(self, ws: typing.Any, raw: str | bytes) -> None:
        """
        Processes an incoming raw WebSocket message and dispatches it to the
        appropriate handler.

        Attempts to decode the message as a JSON envelope containing a message
        type and optional payload. If decoding or payload validation fails, or
        if no handler is registered for the message type, the message is passed
        to the fallback ``on_message`` method. Otherwise, the registered handler
        is invoked with the converted payload.
        """
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
