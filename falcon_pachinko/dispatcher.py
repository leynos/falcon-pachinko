"""Message dispatch helpers for :class:`WebSocketResource`."""

from __future__ import annotations

import dataclasses as dc
import inspect
import logging
import typing as typ

import msgspec as ms
import msgspec.inspect as msinspect
import msgspec.json as msjson

from .exceptions import (
    HandlerNotAsyncError,
    HandlerSignatureError,
    SignatureInspectionError,
)
from .handlers import Handler, HandlerInfo, get_payload_type
from .schema import requires_strict_validation, validate_strict_payload
from .utils import to_snake_case

logger = logging.getLogger(__name__)

if typ.TYPE_CHECKING:  # pragma: no cover - type hints only
    from .protocols import WebSocketLike
    from .resource import WebSocketResource


class Envelope(ms.Struct, frozen=True):
    """Simple envelope for messages without a schema."""

    type: str
    payload: typ.Any | None = None


@dc.dataclass
class HandlerInvocationContext:
    """Context for invoking a message handler."""

    resource: WebSocketResource
    ws: WebSocketLike
    raw: str | bytes
    handler_info: HandlerInfo
    payload: object


def find_conventional_handler(
    resource: WebSocketResource, tag: str
) -> HandlerInfo | None:
    """Return a handler matching ``on_{tag}`` if present."""
    name = f"on_{to_snake_case(tag)}"
    func = getattr(resource.__class__, name, None)
    if func is None or not inspect.iscoroutinefunction(func):
        return None
    try:
        payload_type = get_payload_type(typ.cast("Handler", func))
    except (
        HandlerSignatureError,
        HandlerNotAsyncError,
        SignatureInspectionError,
    ) as exc:
        logger.debug("Handler %s invalid: %s", name, exc)
        return None
    return HandlerInfo(typ.cast("Handler", func), payload_type, strict=True)


async def convert_and_invoke_handler(context: HandlerInvocationContext) -> None:
    """Convert ``payload`` to the handler's type and invoke it."""
    payload_type = context.handler_info.payload_type
    payload = context.payload
    if payload_type is not None and payload is not None:
        try:
            if requires_strict_validation(
                payload, payload_type, strict=context.handler_info.strict
            ):
                validate_strict_payload(
                    payload, payload_type, strict=context.handler_info.strict
                )
            payload = ms.convert(
                payload,
                type=payload_type,
                strict=context.handler_info.strict,
            )
        except ms.ValidationError:
            await context.resource.on_unhandled(context.ws, context.raw)
            return
    await context.handler_info.handler(context.resource, context.ws, payload)


async def dispatch(
    resource: WebSocketResource, ws: WebSocketLike, raw: str | bytes
) -> None:
    """Dispatch ``raw`` to a handler based on :attr:`resource.schema`."""
    if resource.schema is not None:
        await dispatch_with_schema(resource, ws, raw)
    else:
        await dispatch_with_envelope(resource, ws, raw)


async def dispatch_with_schema(
    resource: WebSocketResource, ws: WebSocketLike, raw: str | bytes
) -> None:
    """Decode and dispatch ``raw`` using ``resource.schema``."""
    try:
        message = msjson.decode(raw, type=resource.schema)
    except (ms.DecodeError, ms.ValidationError):
        await resource.on_unhandled(ws, raw)
        return

    entry = resource.__class__._struct_handlers.get(type(message))
    if not entry:
        info = msinspect.type_info(type(message))
        tag_val = typ.cast("msinspect.StructType", info).tag
        conv = find_conventional_handler(resource, typ.cast("str", tag_val))
        if conv is None:
            await resource.on_unhandled(ws, raw)
            return
        ctx = HandlerInvocationContext(resource, ws, raw, conv, message)
        await convert_and_invoke_handler(ctx)
        return

    ctx = HandlerInvocationContext(resource, ws, raw, entry, message)
    await convert_and_invoke_handler(ctx)


async def dispatch_with_envelope(
    resource: WebSocketResource, ws: WebSocketLike, raw: str | bytes
) -> None:
    """Decode and dispatch ``raw`` using the envelope format."""
    try:
        envelope = msjson.decode(raw, type=Envelope)
    except (ms.DecodeError, ms.ValidationError):
        await resource.on_unhandled(ws, raw)
        return

    handler_entry = resource.__class__.handlers.get(envelope.type)
    if handler_entry is None:
        handler_entry = find_conventional_handler(resource, envelope.type)

    if handler_entry is None:
        await resource.on_unhandled(ws, raw)
        return

    ctx = HandlerInvocationContext(resource, ws, raw, handler_entry, envelope.payload)
    await convert_and_invoke_handler(ctx)
