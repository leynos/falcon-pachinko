"""WebSocket resource handling and message dispatching functionality."""

from __future__ import annotations

import collections.abc as cabc
import dataclasses as dc
import functools
import inspect
import re
import typing

import msgspec
import msgspec.inspect as msgspec_inspect
import msgspec.json as msgspec_json

if typing.TYPE_CHECKING:
    import falcon


@dc.dataclass(frozen=True)
class HandlerInfo:
    """Information about a message handler and its payload type."""

    handler: Handler
    payload_type: type | None
    strict: bool = True


def _duplicate_payload_type_msg(
    payload_type: type, handler_name: str | None = None
) -> str:
    """Return a detailed error message for duplicate payload types."""
    msg = f"Duplicate payload type in handlers: {payload_type!r}"
    if handler_name:
        msg += f" (handler: {handler_name})"
    return msg


def _raise_unknown_fields(extra_fields: set[str], payload: dict | None = None) -> None:
    """Raise a validation error for unknown fields."""
    details = f"Unknown fields in payload: {sorted(extra_fields)}"
    if payload is not None:
        details += f" -> {payload}"
    raise msgspec.ValidationError(details)


def _to_snake_case(name: str) -> str:
    """Convert ``name`` to ``snake_case`` as best we can."""
    # Normalize separators to underscores first (e.g., dashes or spaces).
    name = re.sub(r"[^0-9a-zA-Z]+", "_", name)

    # ``sendHTTPMessage`` -> ``send_HTTPMessage``
    name = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", name)

    # ``send_HTTPMessage`` -> ``send_HTTP_Message`` and finally to ``send_http_message``
    name = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", name)

    return name.lower()


class HandlerSignatureError(TypeError):
    """Raised when a handler function has an invalid signature.

    This exception is raised when a handler function doesn't have the required
    parameters (self, ws, and payload) or has an incorrect signature structure.
    """

    def __init__(self, func_name: str) -> None:
        """Initialize the exception with the function name.

        Parameters
        ----------
        func_name : str
            The name of the handler function with invalid signature
        """
        super().__init__(f"Handler {func_name} must accept self, ws, and a payload")


class HandlerNotAsyncError(TypeError):
    """Raised when a handler function is not an async function.

    This exception is raised when attempting to register a handler that is not
    defined as an async function, which is required for WebSocket message handling.
    """

    def __init__(self, func_qualname: str) -> None:
        """Initialize the exception with the function qualified name.

        Parameters
        ----------
        func_qualname : str
            The qualified name of the non-async handler function
        """
        super().__init__(f"Handler {func_qualname} must be async")


class SignatureInspectionError(RuntimeError):
    """Raised when unable to inspect a handler function's signature.

    This exception is raised when the inspect module cannot analyze a handler
    function's signature, typically with C extensions or other special functions.
    """

    def __init__(self, func_qualname: str) -> None:
        """Initialize the exception with the function qualified name.

        Parameters
        ----------
        func_qualname : str
            The qualified name of the function that cannot be inspected
        """
        super().__init__(f"Cannot inspect signature for handler {func_qualname}")


class WebSocketLike(typing.Protocol):
    """Minimal interface for WebSocket connections."""

    async def accept(self, subprotocol: str | None = None) -> None:
        """Accept the WebSocket handshake.

        Parameters
        ----------
        subprotocol : str or None, optional
            The WebSocket subprotocol to use, by default None
        """

    async def close(self, code: int = 1000) -> None:
        """Close the WebSocket connection.

        Parameters
        ----------
        code : int, optional
            The WebSocket close code, by default 1000
        """

    async def send_media(self, data: object) -> None:
        """Send structured data over the connection.

        Parameters
        ----------
        data : object
            The data to send over the WebSocket connection
        """


class _Envelope(msgspec.Struct, frozen=True):
    type: str
    payload: typing.Any | None = None


# Handlers accept ``self``, a ``WebSocketLike`` connection, and a decoded
# payload. The return value is ignored.
Handler = cabc.Callable[[typing.Any, WebSocketLike, typing.Any], cabc.Awaitable[None]]


def _select_payload_param(
    sig: inspect.Signature, *, func_name: str
) -> inspect.Parameter:
    """Return the parameter representing the message payload.

    Parameters
    ----------
    sig : inspect.Signature
        The function signature to analyze
    func_name : str
        The name of the function for error messages

    Returns
    -------
    inspect.Parameter
        The parameter representing the message payload
    """
    params = list(sig.parameters.values())
    if len(params) < 3:
        raise HandlerSignatureError(func_name)

    payload_param = sig.parameters.get("payload")
    if payload_param is None:
        for candidate in params[2:]:
            if candidate.annotation is not inspect.Signature.empty:
                return candidate
        payload_param = params[2]

    return payload_param


def _get_payload_type(func: Handler) -> type | None:
    """Validate ``func``'s signature and return the payload annotation.

    Parameters
    ----------
    func : Handler
        The handler function to analyze

    Returns
    -------
    type or None
        The payload type annotation, or None if not found
    """
    if not inspect.iscoroutinefunction(func):
        raise HandlerNotAsyncError(func.__qualname__)

    try:
        sig = inspect.signature(func)
    except ValueError as exc:  # pragma: no cover - C extensions unlikely
        raise SignatureInspectionError(func.__qualname__) from exc

    param = _select_payload_param(sig, func_name=func.__qualname__)
    try:
        hints: dict[str, type] = typing.get_type_hints(func)
    except (NameError, AttributeError):
        hints = {}

    return hints.get(param.name)


class _HandlesMessageDescriptor:
    """Register a method as a message handler on its class."""

    def __init__(
        self, message_type: str, func: Handler, *, strict: bool = True
    ) -> None:
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
        self.payload_type = _get_payload_type(func)
        self.strict = strict
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

        typed_owner.add_handler(
            self.message_type,
            self.func,
            payload_type=self.payload_type,
            strict=self.strict,
        )

    def __get__(
        self, instance: object, owner: type | None = None
    ) -> Handler | _HandlesMessageDescriptor:  # type: ignore[override]
        """Return the bound handler when accessed via an instance.

        Parameters
        ----------
        instance : object
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
    *,
    strict: bool = True,
) -> cabc.Callable[[Handler], _HandlesMessageDescriptor]:
    """Create a decorator to mark a method as a WebSocket message handler.

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
        return _HandlesMessageDescriptor(message_type, func, strict=strict)

    return decorator


class WebSocketResource:
    """Base class for WebSocket handlers.

    Subclasses may optionally define a :attr:`schema` attribute referencing a
    :func:`typing.Union` of :class:`msgspec.Struct` types. When provided,
    incoming messages are decoded using this tagged union and dispatched based
    on the message tag. This enables high-performance, schema-driven routing
    without additional boilerplate.
    """

    handlers: typing.ClassVar[dict[str, HandlerInfo]]
    _struct_handlers: typing.ClassVar[dict[type, HandlerInfo]] = {}
    schema: type | None = None

    def __init_subclass__(cls, **kwargs: object) -> None:
        """Initialize and merge handler mappings for subclasses."""
        super().__init_subclass__(**kwargs)

        existing = getattr(cls, "handlers", {})
        handlers = cls._collect_base_handlers()
        handlers.update(existing)
        cls._apply_overrides(handlers)
        cls.handlers = handlers
        cls._init_schema_registry()

    @classmethod
    def _collect_base_handlers(
        cls,
    ) -> dict[str, HandlerInfo]:
        """Gather handler mappings from base classes."""
        combined: dict[str, HandlerInfo] = {}
        for base in cls.__mro__[1:]:
            base_handlers = getattr(base, "handlers", None)
            if base_handlers:
                combined.update(base_handlers)
        return combined

    @classmethod
    def _apply_overrides(cls, handlers: dict[str, HandlerInfo]) -> None:
        """Update ``handlers`` when methods are overridden in ``cls``."""
        shadowed = {
            name
            for name, obj in cls.__dict__.items()
            if not isinstance(obj, _HandlesMessageDescriptor)
            and inspect.iscoroutinefunction(obj)
        }

        for msg_type, info in list(handlers.items()):
            if info.handler.__name__ in shadowed:
                new_handler = typing.cast(
                    "Handler", cls.__dict__[info.handler.__name__]
                )
                handlers[msg_type] = HandlerInfo(
                    new_handler, info.payload_type, info.strict
                )

    @classmethod
    def _init_schema_registry(cls) -> None:
        """Validate :attr:`schema` and populate the struct handler map."""
        cls._struct_handlers = {}

        schema = getattr(cls, "schema", None)
        if schema is None:
            return

        cls._validate_schema_types(schema)
        cls._populate_struct_handlers()

    @classmethod
    def _validate_schema_types(cls, schema: type) -> None:
        """Ensure all schema types are :class:`msgspec.Struct` with tags."""
        types = typing.get_args(schema) or (schema,)
        for t in types:
            if not (inspect.isclass(t) and issubclass(t, msgspec.Struct)):
                raise TypeError("schema must contain only msgspec.Struct types")  # noqa: TRY003

            info = msgspec_inspect.type_info(t)
            if typing.cast("msgspec_inspect.StructType", info).tag is None:
                raise TypeError("schema Struct types must define a tag")  # noqa: TRY003

    @classmethod
    def _populate_struct_handlers(cls) -> None:
        """Create mapping of struct types to handlers."""
        for info in cls.handlers.values():
            handler = info.handler
            payload_type = info.payload_type
            if payload_type is None or not issubclass(payload_type, msgspec.Struct):
                continue

            existing = cls._struct_handlers.get(payload_type)
            if existing is not None:
                raise ValueError(
                    _duplicate_payload_type_msg(payload_type, handler.__qualname__)
                )
            cls._struct_handlers[payload_type] = info

    def _find_conventional_handler(self, tag: str) -> HandlerInfo | None:
        """Return a handler matching ``on_{tag}`` if present."""
        name = f"on_{_to_snake_case(tag)}"
        func = getattr(self.__class__, name, None)
        if func is None or not inspect.iscoroutinefunction(func):
            return None
        try:
            payload_type = _get_payload_type(typing.cast("Handler", func))
        except (HandlerSignatureError, HandlerNotAsyncError, SignatureInspectionError):
            return None

        return HandlerInfo(typing.cast("Handler", func), payload_type, strict=True)

    def _should_validate_extra_fields(
        self,
        handler_info: HandlerInfo,
        payload: object,
        payload_type: type,
    ) -> bool:
        """Return ``True`` if strict validation of extra fields is required."""
        return (
            handler_info.strict
            and isinstance(payload, dict)
            and issubclass(payload_type, msgspec.Struct)
        )

    def _validate_strict_payload(
        self, payload: object, payload_type: type, *, strict: bool
    ) -> None:
        """Raise if ``payload`` contains unknown fields in strict mode."""
        if (
            strict
            and isinstance(payload, dict)
            and issubclass(payload_type, msgspec.Struct)
        ):
            info = msgspec_inspect.type_info(payload_type)
            allowed = {
                f.name for f in typing.cast("msgspec_inspect.StructType", info).fields
            }
            extra = set(payload) - allowed
            if extra:
                _raise_unknown_fields(extra, payload)

    async def on_connect(
        self, req: falcon.Request, ws: WebSocketLike, **params: object
    ) -> bool:
        """Decide whether the connection should be accepted after handshake.

        Called after the WebSocket handshake is complete to determine acceptance.

        Parameters
        ----------
        req : falcon.Request
            The incoming HTTP request associated with the WebSocket handshake
        ws : WebSocketLike
            The WebSocket connection object
        **params : object
            Additional parameters relevant to the connection

        Returns
        -------
        bool
            True to accept the WebSocket connection; False to reject it
        """
        return True

    async def on_disconnect(self, ws: WebSocketLike, close_code: int) -> None:
        """Handle cleanup or custom logic when the WebSocket connection is closed.

        Parameters
        ----------
        ws : WebSocketLike
            The WebSocket connection instance
        close_code : int
            The close code indicating the reason for disconnection
        """

    async def on_message(self, ws: WebSocketLike, message: str | bytes) -> None:
        """Handle incoming WebSocket messages that do not match any registered handler.

        Called when a message cannot be decoded or its type is unrecognized.
        Override to implement custom fallback behavior for such messages.

        Parameters
        ----------
        ws : WebSocketLike
            The WebSocket connection instance
        message : str or bytes
            The raw message received
        """

    @classmethod
    def add_handler(
        cls,
        message_type: str,
        handler: Handler,
        *,
        payload_type: type | None = None,
        strict: bool = True,
    ) -> None:
        """Register a handler function for a specific message type.

        Associates the given handler with the specified message type.
        Optionally, a payload type can be provided for automatic payload
        validation and conversion.

        Parameters
        ----------
        message_type : str
            The message type to handle
        handler : Handler
            The handler function to register
        payload_type : type or None, optional
            The payload type for automatic validation and conversion, by default None
        """
        cls.handlers[message_type] = HandlerInfo(handler, payload_type, strict)

    async def dispatch(self, ws: WebSocketLike, raw: str | bytes) -> None:
        """Process an incoming raw WebSocket message and dispatch it.

        If :attr:`schema` is defined, ``raw`` is decoded using that schema and
        routed based on the resulting message's type. Otherwise, the message is
        interpreted as a JSON envelope with ``type`` and optional ``payload``
        fields. Any decoding failure or missing handler results in a call to
        :meth:`on_message`.

        Parameters
        ----------
        ws : WebSocketLike
            The WebSocket connection instance
        raw : str or bytes
            The raw message to process and dispatch
        """
        if self.schema is not None:
            await self._dispatch_with_schema(ws, raw)
        else:
            await self._dispatch_with_envelope(ws, raw)

    async def _dispatch_with_schema(self, ws: WebSocketLike, raw: str | bytes) -> None:
        """Decode and dispatch ``raw`` using :attr:`schema`."""
        try:
            message = msgspec_json.decode(raw, type=self.schema)
        except (msgspec.DecodeError, msgspec.ValidationError):
            await self.on_message(ws, raw)
            return

        entry = self.__class__._struct_handlers.get(type(message))
        if not entry:
            info = msgspec_inspect.type_info(type(message))
            tag = typing.cast("msgspec_inspect.StructType", info).tag
            conv = self._find_conventional_handler(tag)
            if conv is None:
                await self.on_message(ws, raw)
                return
            await self._convert_and_invoke_handler(ws, raw, conv, message)
            return

        await self._convert_and_invoke_handler(ws, raw, entry, message)

    async def _convert_and_invoke_handler(
        self,
        ws: WebSocketLike,
        raw: str | bytes,
        handler_info: HandlerInfo,
        payload: object,
    ) -> None:
        """Convert ``payload`` to the handler's type and invoke it."""
        payload_type = handler_info.payload_type
        if payload_type is not None and payload is not None:
            try:
                if self._should_validate_extra_fields(
                    handler_info,
                    payload,
                    payload_type,
                ):
                    self._validate_strict_payload(
                        payload, payload_type, strict=handler_info.strict
                    )

                payload = typing.cast(
                    "typing.Any",
                    msgspec.convert(
                        payload,
                        type=payload_type,
                        strict=handler_info.strict,
                    ),
                )
            except msgspec.ValidationError:
                await self.on_message(ws, raw)
                return

        await handler_info.handler(self, ws, payload)

    async def _dispatch_with_envelope(
        self, ws: WebSocketLike, raw: str | bytes
    ) -> None:
        """Decode and dispatch ``raw`` using the envelope format."""
        try:
            envelope = msgspec_json.decode(raw, type=_Envelope)
        except msgspec.DecodeError:
            await self.on_message(ws, raw)
            return

        handler_entry = self.__class__.handlers.get(envelope.type)
        if handler_entry is None:
            handler_entry = self._find_conventional_handler(envelope.type)

        if handler_entry is None:
            await self.on_message(ws, raw)
            return

        await self._convert_and_invoke_handler(ws, raw, handler_entry, envelope.payload)
