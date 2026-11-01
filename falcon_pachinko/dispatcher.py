"""Message dispatch helpers for :class:`WebSocketResource`."""

from __future__ import annotations

import dataclasses as dc
import inspect
import logging
import typing as typ

import msgspec as ms
import msgspec.json as msjson

from .diagnostics import _class_name, _frame_metadata, _identifier, _type_name
from .exceptions import (
    HandlerNotAsyncError,
    HandlerSignatureError,
    SignatureInspectionError,
)
from .handlers import LIFECYCLE_CALLBACK_NAMES, HandlerInfo, get_payload_type
from .schema import schema_dispatch, validate_strict_payload
from .utils import to_snake_case

logger = logging.getLogger(__name__)

if typ.TYPE_CHECKING:  # pragma: no cover - type hints only
    from .protocols import WebSocketLike
    from .resource import WebSocketResource


class Envelope(ms.Struct, frozen=True):
    """Simple envelope for messages without a schema."""

    type: str
    payload: typ.Any | None = None


@dc.dataclass(slots=True, repr=False)
class HandlerInvocationContext:
    """Context for invoking a message handler."""

    resource: WebSocketResource
    ws: WebSocketLike
    raw: str | bytes
    handler_info: HandlerInfo
    payload: object
    #: Struct selected by schema dispatch, when it differs from the handler's
    #: annotation. Schema mode always converts to the resolved member so an
    #: unannotated conventional handler still receives a typed payload.
    target_type: type | None = None

    def __repr__(self) -> str:
        """Describe the invocation without displaying raw or decoded values."""
        kind, length = _frame_metadata(self.raw)
        return (
            f"HandlerInvocationContext(resource={_type_name(self.resource)}, "
            f"ws={_type_name(self.ws)}, raw_kind={kind}, raw_length={length}, "
            f"handler_info={_type_name(self.handler_info)}, "
            f"expected_type={_expected_type_label(self.handler_info)}, "
            f"payload_type={_type_name(self.payload)})"
        )


def _expected_type_label(info: object) -> str:
    """Select schema metadata from an exact framework handler record.

    Parameters
    ----------
    info : object
        A handler record whose caller-controlled display methods are ignored.

    Returns
    -------
    str
        The bounded schema name, or an omission marker for unsupported records.
    """
    if type(info) is HandlerInfo and info.payload_type is not None:
        return _class_name(info.payload_type)
    return "<omitted>"


def find_conventional_handler(
    resource: WebSocketResource, tag: str
) -> HandlerInfo | None:
    """Return a handler matching ``on_{tag}`` if the class allows that name.

    Only names in ``_conventional_handler_names`` are considered. That
    allowlist is built once when the class is created and excludes the
    lifecycle callbacks, so a peer-chosen tag cannot reach ``on_connect``,
    ``on_accepted``, ``on_disconnect`` or ``on_unhandled``, nor an alias of one.
    A class without the attribute (a non-monitored stand-in) offers no
    conventional handlers at all.

    Returns
    -------
    HandlerInfo | None
        The discovered handler, or ``None`` when the class exposes no usable
        conventional handler for ``tag``.
    """
    name = f"on_{to_snake_case(tag)}"
    allowed = getattr(resource.__class__, "_conventional_handler_names", frozenset())
    if name not in allowed:
        if name in LIFECYCLE_CALLBACK_NAMES:
            # The refusal is silent for an ordinary unknown tag, but a peer
            # reaching for a reserved callback is a security-relevant signal
            # worth making observable. Only the normalised name is logged:
            # it is one of four module constants, so rendering it through
            # ``_identifier`` cannot disclose peer-supplied text.
            logger.debug(
                "Refused conventional handler %s (reason=reserved_lifecycle_tag)",
                _identifier(name),
            )
        return None
    func = getattr(resource.__class__, name, None)
    if func is None or not inspect.iscoroutinefunction(func):
        return None
    try:
        payload_type = get_payload_type(func)
    except (
        HandlerSignatureError,
        HandlerNotAsyncError,
        SignatureInspectionError,
    ) as exc:
        logger.debug("Handler %s invalid: %s", _identifier(name), _type_name(exc))
        return None
    return HandlerInfo(func, payload_type, strict=True)


def _convert_payload(context: HandlerInvocationContext, payload_type: type) -> object:
    """Convert the retained payload, or re-decode a schema frame from the wire.

    Schema dispatch decodes frames generically so undeclared keys can be
    inspected. That mapping has already lost wire-level detail: integer mapping
    keys arrive as strings, ``msgspec.Raw`` fields arrive as decoded JSON, and
    ``Decimal`` fields pass through a ``float``. Re-decoding the original frame
    into the resolved member restores msgspec's wire semantics.

    A ``strict=False`` handler keeps its documented lax coercion: when the wire
    decode rejects a value the lax rules would accept, such as the string
    ``"5"`` in an integer field, the already-decoded mapping is converted
    instead.

    Parameters
    ----------
    context : HandlerInvocationContext
        The invocation carrying the retained payload and the original frame.
    payload_type : type
        The type to convert into.

    Returns
    -------
    object
        The converted payload.

    Raises
    ------
    msgspec.ValidationError
        If the payload does not satisfy ``payload_type``.
    """
    if context.target_type is None:
        return ms.convert(
            context.payload,
            type=payload_type,
            strict=context.handler_info.strict,
        )
    try:
        return msjson.decode(context.raw, type=payload_type)
    except ms.ValidationError:
        if context.handler_info.strict:
            raise
    # Convert after leaving the ``except`` suite so a failure here does not
    # retain the wire-decode error as its ``__context__``: the vendor message
    # can quote peer-supplied field names.
    return ms.convert(
        context.payload,
        type=payload_type,
        strict=context.handler_info.strict,
    )


async def convert_and_invoke_handler(context: HandlerInvocationContext) -> None:
    """Convert ``payload`` to the handler's type and invoke it."""
    payload_type = (
        context.target_type
        if context.target_type is not None
        else context.handler_info.payload_type
    )
    payload = context.payload
    failed = False
    if payload_type is not None and payload is not None:
        try:
            validate_strict_payload(
                payload, payload_type, strict=context.handler_info.strict
            )
            payload = _convert_payload(context, payload_type)
        except ms.ValidationError:
            failed = True
    if failed:
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


def _decode_schema_frame(raw: str | bytes) -> dict[str, object] | None:
    """Decode ``raw`` to a JSON object, or return ``None`` when it is not one.

    Returning from inside the ``except`` suite keeps the vendor decode error out
    of ``__context__``: callers fall back outside it, so no application error is
    raised while that exception is being handled. The decode error can quote
    the frame, so it must not be chained to a reply.

    Parameters
    ----------
    raw : str | bytes
        The received frame.

    Returns
    -------
    dict[str, object] | None
        The decoded JSON object, or ``None`` when the frame is malformed or is
        not a JSON object.
    """
    try:
        payload = msjson.decode(raw)
    except (ms.DecodeError, ms.ValidationError):
        return None
    match payload:
        case dict():
            return typ.cast("dict[str, object]", payload)
        case _:
            return None


async def dispatch_with_schema(
    resource: WebSocketResource, ws: WebSocketLike, raw: str | bytes
) -> None:
    """Decode and dispatch ``raw`` using ``resource.schema``.

    The frame is decoded generically to a mapping first. Decoding straight into
    the schema union would let ``msgspec`` discard undeclared fields before the
    strict decision, so a strict handler could receive a message that violated
    its no-extra-fields contract. The mapping is retained through validation and
    the original frame is then decoded into the resolved Struct, which keeps
    wire-level values the generic mapping cannot carry.

    ``None`` tags, unknown tags, non-object frames and malformed JSON fall back
    to ``on_unhandled``. A configuration ``TypeError`` from an invalid schema
    propagates instead.
    """
    # ``dispatch`` only routes here when a schema is set. Resolve the tag table
    # first so a misconfigured schema raises for every frame, good or bad,
    # rather than surfacing only once a well-formed object arrives.
    schema = typ.cast("type", resource.schema)
    table = schema_dispatch(schema)
    payload = _decode_schema_frame(raw)
    if payload is None:
        await resource.on_unhandled(ws, raw)
        return

    resolution = table.resolve(payload)
    if resolution is None:
        await resource.on_unhandled(ws, raw)
        return
    member, tag = resolution

    entry = resource.__class__._struct_handlers.get(member)
    if entry is None:
        # msgspec permits integer tags; conventional lookup uses strings.
        entry = find_conventional_handler(resource, str(tag))
    if entry is None:
        await resource.on_unhandled(ws, raw)
        return

    ctx = HandlerInvocationContext(resource, ws, raw, entry, payload, member)
    await convert_and_invoke_handler(ctx)


async def dispatch_with_envelope(
    resource: WebSocketResource, ws: WebSocketLike, raw: str | bytes
) -> None:
    """Decode and dispatch ``raw`` using the envelope format."""
    failed = False
    try:
        envelope = msjson.decode(raw, type=Envelope)
    except (ms.DecodeError, ms.ValidationError):
        failed = True
    if failed:
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
