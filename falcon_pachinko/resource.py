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


def _select_payload_param(
    sig: inspect.Signature, *, func_name: str
) -> inspect.Parameter:
    """Return the parameter representing the message payload."""

    params = list(sig.parameters.values())
    if len(params) < 3:
        raise TypeError(f"Handler {func_name} must accept self, ws, and a payload")

    payload_param = sig.parameters.get("payload")
    if payload_param is None:
        for candidate in params[2:]:
            if candidate.annotation is not inspect.Signature.empty:
                return candidate
        payload_param = params[2]

    return payload_param


def _get_payload_type(func: Handler) -> type | None:
    """Validate ``func``'s signature and return the payload annotation."""

    if not inspect.iscoroutinefunction(func):
        raise TypeError(f"Handler {func.__qualname__} must be async")

    try:
        sig = inspect.signature(func)
    except ValueError as exc:  # pragma: no cover - C extensions unlikely
        raise RuntimeError(
            f"Cannot inspect signature for handler {func.__qualname__}"
        ) from exc

    param = _select_payload_param(sig, func_name=func.__qualname__)
    try:
        hints: dict[str, type] = typing.get_type_hints(func)
    except (NameError, AttributeError):
        hints = {}

    return hints.get(param.name)


class _HandlesMessageDescriptor:
    """Register a method as a message handler on its class."""

    def __init__(self, message_type: str, func: Handler) -> None:
        """Store metadata about a message handler.

        Parameters
        ----------
        message_type : str
            The message ``type`` this handler responds to.
        func : Handler
            The coroutine implementing the handler logic.

        Notes
        -----
        This descriptor merely records the handler and ``message_type`` until
        ``__set_name__`` adds it to the class-level ``handlers`` registry used
        by :meth:`WebSocketResource.dispatch`. Keeping the registry update
        separate allows subclasses to define their own handlers independently.
        """

        self.message_type = message_type
        self.func = func
        functools.update_wrapper(self, func)  # pyright: ignore[reportArgumentType]
        self.owner: type | None = None
        self.name: str | None = None

    def __set_name__(self, owner: type, name: str) -> None:
        """Register the handler with ``owner`` when the class is created.

        Parameters
        ----------
        owner : type
            The class declaring the handler.
        name : str
            The attribute name under which the handler is defined.

        Raises
        ------
        RuntimeError
            If ``owner.add_handler`` raises a ``RuntimeError``, for example if
            ``owner`` already defines a handler for ``message_type``.

        Notes
        -----
        Invoked automatically during class creation, this method inserts the
        handler into ``owner.handlers`` by calling ``owner.add_handler``. The
        registry maps message types to handlers and optional payload types so
        that :meth:`WebSocketResource.dispatch` can quickly find and invoke the
        correct coroutine.
        """

        self.owner = owner
        self.name = name

        typed_owner = typing.cast("type[WebSocketResource]", owner)
        current = typed_owner.__dict__.get("handlers")
        if current is None:
            current = {}
            typed_owner.handlers = current
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

    def __get__(
        self, instance: typing.Any, owner: type | None = None
    ) -> Handler | _HandlesMessageDescriptor:  # type: ignore[override]
        """Return the bound handler when accessed via an instance.

        Parameters
        ----------
        instance : Any
            The :class:`WebSocketResource` instance or ``None`` when accessed on
            the class.
        owner : type, optional
            The class owning the descriptor.

        Returns
        -------
        Handler | _HandlesMessageDescriptor
            The bound coroutine if accessed through ``instance``; otherwise the
            descriptor itself.

        Notes
        -----
        ``dispatch`` looks up handlers in the ``handlers`` registry and then
        calls ``__get__`` on the descriptor to obtain the bound coroutine to
        invoke.
        """

        if instance is None:
            return self
        return self.func.__get__(instance, owner or self.owner)


def handles_message(
    message_type: str,
) -> cabc.Callable[[Handler], _HandlesMessageDescriptor]:
    """Decorator factory to mark a method as a WebSocket message handler.

    Parameters
    ----------
    message_type : str
        The value of the ``type`` field for messages this handler should
        process.

    Examples
    --------
    Basic usage::

        class MyResource(WebSocketResource):
            @handles_message("ping")
            async def handle_ping(self, ws, payload):
                await ws.send_text("pong")

    Typed payloads can be declared using ``msgspec.Struct``::

        class Status(msgspec.Struct):
            text: str

        class StatusResource(WebSocketResource):
            @handles_message("status")
            async def update_status(self, ws, payload: Status) -> None:
                print(payload.text)

    Returns
    -------
    Callable[[Handler], _HandlesMessageDescriptor]
        A descriptor that registers the decorated coroutine as a handler for
        ``message_type`` when the class is created.
    """

    def decorator(func: Handler) -> _HandlesMessageDescriptor:
        return _HandlesMessageDescriptor(message_type, func)

    return decorator


class WebSocketResource:
    """Base class for WebSocket handlers."""

    handlers: typing.ClassVar[dict[str, tuple[Handler, type | None]]]

    def __init_subclass__(cls, **kwargs: typing.Any) -> None:
        """Initialize and merge handler mappings for subclasses."""
        super().__init_subclass__(**kwargs)

        existing = getattr(cls, "handlers", {})
        handlers = cls._collect_base_handlers()
        handlers.update(existing)
        cls._apply_overrides(handlers)
        cls.handlers = handlers

    @classmethod
    def _collect_base_handlers(
        cls,
    ) -> dict[str, tuple[Handler, type | None]]:
        """Gather handler mappings from base classes."""

        combined: dict[str, tuple[Handler, type | None]] = {}
        for base in cls.__mro__[1:]:
            base_handlers = getattr(base, "handlers", None)
            if base_handlers:
                combined.update(base_handlers)
        return combined

    @classmethod
    def _apply_overrides(cls, handlers: dict[str, tuple[Handler, type | None]]) -> None:
        """Update ``handlers`` when methods are overridden in ``cls``."""

        shadowed = {
            name
            for name, obj in cls.__dict__.items()
            if not isinstance(obj, _HandlesMessageDescriptor)
            and inspect.iscoroutinefunction(obj)
        }

        for msg_type, (handler, payload_type) in list(handlers.items()):
            if handler.__name__ in shadowed:
                new_handler = typing.cast("Handler", cls.__dict__[handler.__name__])
                handlers[msg_type] = (new_handler, payload_type)

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
