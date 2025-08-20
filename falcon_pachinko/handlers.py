"""Utilities for registering and validating message handlers."""

from __future__ import annotations

import collections.abc as cabc
import dataclasses as dc
import functools
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
from .protocols import WebSocketLike

# Handlers accept ``self``, a ``WebSocketLike`` connection, and a decoded payload.
# The return value is ignored.
Handler = cabc.Callable[[typ.Any, WebSocketLike, typ.Any], cabc.Awaitable[None]]


@dc.dataclass(frozen=True)
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
    if len(params) < 3:
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
    """Validate ``func``'s signature and return the payload annotation."""
    if not inspect.iscoroutinefunction(func):
        raise HandlerNotAsyncError(func.__qualname__)

    try:
        sig = inspect.signature(func)
    except ValueError as exc:  # pragma: no cover - C extensions unlikely
        raise SignatureInspectionError(func.__qualname__) from exc

    param = select_payload_param(sig, func_name=func.__qualname__)
    try:
        hints: dict[str, type] = typ.get_type_hints(func)
    except (NameError, AttributeError):
        hints = {}
    return hints.get(param.name)


class _HandlesMessageDescriptor:
    """Register a method as a message handler on its class."""

    def __init__(
        self, message_type: str, func: Handler, *, strict: bool = True
    ) -> None:
        self.message_type = message_type
        self.func = func
        self.payload_type = get_payload_type(func)
        self.strict = strict
        functools.update_wrapper(self, func)
        self.owner: type | None = None
        self.name: str | None = None

    def __set_name__(self, owner: type, name: str) -> None:
        self.owner = owner
        self.name = name

        typed_owner = typ.cast("type[WebSocketResource]", owner)
        current = typed_owner.__dict__.get("handlers")
        if current is None:
            current = {}
            typed_owner.handlers = current
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
        return self.func.__get__(instance, owner or self.owner)


def handles_message(
    message_type: str, *, strict: bool = True
) -> cabc.Callable[[Handler], _HandlesMessageDescriptor]:
    """Create a decorator to mark a method as a WebSocket message handler."""

    def decorator(func: Handler) -> _HandlesMessageDescriptor:
        return _HandlesMessageDescriptor(message_type, func, strict=strict)

    return decorator
