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


def _get_payload_type(func: Handler) -> type | None:
    """Validate ``func``'s signature and return the payload annotation."""

    try:
        sig = inspect.signature(func)
    except ValueError as exc:  # pragma: no cover - C extensions unlikely
        raise RuntimeError(
            f"Cannot inspect signature for handler {func.__qualname__}"
        ) from exc

    params = sig.parameters
    if len(params) < 3:
        raise TypeError(f"Handler {func.__qualname__} must accept self, ws, payload")

    payload_param = params.get("payload")
    if payload_param is None:
        payload_param = list(params.values())[2]

    hints: dict[str, type] = typing.get_type_hints(func)
    return hints.get(payload_param.name)


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
        if "handlers" not in typed_owner.__dict__:
            typed_owner.handlers = {}

        current = typed_owner.__dict__.get("handlers", {})
        if self.message_type in current:
            msg = (
                f"Duplicate handler for message type {self.message_type!r} "
                f"on {owner.__qualname__}"
            )
            raise RuntimeError(msg)

        payload_type = _get_payload_type(self.func)

        typed_owner.add_handler(
            self.message_type,
            self.func,
            payload_type=payload_type,
        )

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

        existing = getattr(cls, "handlers", {})
        combined: dict[str, tuple[Handler, type | None]] = {}
        for base in cls.__mro__[1:]:
            base_handlers = getattr(base, "handlers", None)
            if base_handlers:
                combined.update(base_handlers)
        combined.update(existing)

        shadowed = {
            name
            for name, obj in cls.__dict__.items()
            if not isinstance(obj, _HandlesMessageDescriptor)
            and inspect.iscoroutinefunction(obj)
        }

        for msg_type, (handler, payload_type) in list(combined.items()):
            if handler.__name__ in shadowed:
                new_handler = typing.cast("Handler", cls.__dict__[handler.__name__])
                combined[msg_type] = (new_handler, payload_type)

        cls.handlers = combined

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
