"""Utilities for registering and validating message handlers.

Registration rejects a handler that implements a reserved lifecycle callback
so that a peer-chosen tag can never reach ``on_connect``, ``on_disconnect`` or
``on_unhandled``. The check recognises a lifecycle callback by its name and by
identity against the class it was defined on, including through
``functools.partial`` wrappers.

A wrapper then, that *calls* a lifecycle method rather than *being* one reads
as an ordinary handler:

```python
async def sneaky(self, ws, payload):  # not recognised as a callback
    await self.on_disconnect(ws, 1000)
```

Registering such a wrapper from application code is the same act as calling
the lifecycle method directly, so it confers no capability the application did
not already have — it cannot be told apart from a legitimate handler that
happens to consult lifecycle state. What the check does prevent is a
conventional name or an alias *drifting* into the peer-reachable registry.
"""

from __future__ import annotations

import collections.abc as cabc
import dataclasses as dc
import inspect
import typing as typ

if typ.TYPE_CHECKING:
    from .resource import WebSocketResource

from .exceptions import (
    DuplicateHandlerRegistrationError,
    HandlerNotAsyncError,
    HandlerSignatureError,
    SignatureInspectionError,
)

# Handlers accept ``self``, a ``WebSocketLike`` connection, and a decoded payload.
# The return value is ignored.
type Handler = cabc.Callable[..., cabc.Awaitable[None]]

# ``self``, the connection, and the payload are the minimum handler parameters.
_MIN_HANDLER_PARAMS = 3


class _BindableHandler(typ.Protocol):
    """Descriptor surface of the function objects registered as handlers."""

    def __get__(self, instance: object, owner: type | None = None, /) -> Handler:
        """Bind the handler to ``instance``."""


@dc.dataclass(frozen=True, slots=True)
class HandlerInfo:
    """Information about a message handler and its payload type."""

    handler: Handler
    payload_type: type | None
    strict: bool = True


def select_payload_param(
    sig: inspect.Signature, *, func_name: str
) -> inspect.Parameter:
    """Return the parameter representing the message payload."""
    params = list(sig.parameters.values())
    if len(params) < _MIN_HANDLER_PARAMS:
        raise HandlerSignatureError(func_name)

    payload_param = sig.parameters.get("payload")
    if payload_param is None:
        annotated_candidates = [
            c for c in params[2:] if c.annotation is not inspect.Signature.empty
        ]
        if len(annotated_candidates) > 1:
            msg = (
                f"Ambiguous payload parameter in handler '{func_name}': "
                "multiple annotated parameters found after the first two."
            )
            raise HandlerSignatureError(msg)
        if len(annotated_candidates) == 1:
            return annotated_candidates[0]
        payload_param = params[2]
    return payload_param


def get_payload_type(func: Handler) -> type | None:
    """Validate ``func``'s signature and return its resolved annotation unchanged.

    The annotation is returned without class validation.
    An invalid payload signature raises ``HandlerSignatureError``.

    Returns
    -------
    type | None
        The resolved payload annotation, or ``None`` when it is unavailable.

    Raises
    ------
    HandlerNotAsyncError
        If ``func`` is not a coroutine function.
    SignatureInspectionError
        If Python cannot inspect ``func``'s signature.
    HandlerSignatureError
        If ``func`` does not identify exactly one payload parameter.
    """  # ruff: ignore[docstring-extraneous-exception] - propagated
    func_name: str = getattr(func, "__qualname__", repr(func))
    if not inspect.iscoroutinefunction(func):
        raise HandlerNotAsyncError(func_name)

    try:
        sig = inspect.signature(func)
    except ValueError as exc:  # pragma: no cover - C extensions unlikely
        raise SignatureInspectionError(func_name) from exc

    param = select_payload_param(sig, func_name=func_name)
    try:
        hints: dict[str, object] = typ.get_type_hints(func)
    except (NameError, AttributeError):
        hints = {}
    # cast: ``get_type_hints`` may return non-class typing objects, including
    # unions and parameterized generics. This preserves the ``type | None``
    # contract expected by ``HandlerInfo.payload_type`` and ``schema.py``.
    return typ.cast("type | None", hints.get(param.name))


class _HandlesMessageDescriptor:
    """Register a method as a message handler on its class."""

    def __init__(
        self, message_type: str, func: Handler, *, strict: bool = True
    ) -> None:
        self.message_type = message_type
        self.func = func
        self.payload_type = get_payload_type(func)
        self.strict = strict
        self.owner: type | None = None
        self.name: str | None = None

    def __set_name__(self, owner: type, name: str) -> None:
        self.owner = owner
        self.name = name

        # cast: the descriptor protocol fixes ``owner`` as ``type``, but the
        # decorator is only meaningful on WebSocketResource subclasses.
        typed_owner = typ.cast("type[WebSocketResource]", owner)
        current = typed_owner.__dict__.get("handlers")
        if current is None:
            typed_owner.handlers = current = {}
        if self.message_type in current:
            msg = (
                f"Duplicate handler for message type {self.message_type!r} "
                f"on {owner.__qualname__}"
            )
            raise DuplicateHandlerRegistrationError(msg)

        typed_owner.add_handler(
            self.message_type,
            self.func,
            payload_type=self.payload_type,
            strict=self.strict,
        )

    def __get__(
        self, instance: object, owner: type | None = None
    ) -> Handler | _HandlesMessageDescriptor:
        if instance is None:
            return self
        # cast: ``Handler`` is a bare callable alias, so the checker cannot see
        # the descriptor protocol of the underlying function object.
        binder = typ.cast("_BindableHandler", self.func)
        return binder.__get__(instance, owner or self.owner)


def handles_message(
    message_type: str, *, strict: bool = True
) -> cabc.Callable[
    [cabc.Callable[..., cabc.Awaitable[None]]], _HandlesMessageDescriptor
]:
    """Create a decorator to mark a method as a WebSocket message handler."""

    def decorator(
        func: cabc.Callable[..., cabc.Awaitable[None]],
    ) -> _HandlesMessageDescriptor:
        return _HandlesMessageDescriptor(message_type, func, strict=strict)

    return decorator
