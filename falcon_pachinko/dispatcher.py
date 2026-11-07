"""Message dispatch helpers for :class:`WebSocketResource`."""

from __future__ import annotations

import dataclasses as dc
import decimal
import inspect
import logging
import time
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

# Generic schema inspection needs only the frame's keys and tag value, but a
# default decoder parses every JSON float as a binary ``float``. A literal such
# as ``1e400`` is out of float range yet valid for a ``Decimal`` or
# ``msgspec.Raw`` field, and the default decoder rejects it before the typed
# wire decode can preserve the value. Decoding inspection floats as ``Decimal``
# keeps every literal exact and unbounded.
_INSPECTION_DECODER = msjson.Decoder(float_hook=decimal.Decimal)

# Bounded outcome labels for schema-dispatch observations. Keeping the set here
# means an unexpected caller value becomes ``other`` rather than an unbounded
# log field.
_SCHEMA_DISPATCH_OUTCOMES = frozenset({
    "delivered",
    "malformed",
    "unknown_tag",
    "no_handler",
    "rejected_fields",
    "conversion_failed",
    "configuration_error",
})

type _DeliveryOutcome = typ.Literal["delivered", "rejected_fields", "conversion_failed"]

if typ.TYPE_CHECKING:  # pragma: no cover - type hints only
    import collections.abc as cabc

    from .protocols import WebSocketLike
    from .resource import WebSocketResource


def _log_schema_dispatch(
    resource: WebSocketResource,
    outcome: str,
    *,
    handler_mode: str | None = None,
    strict: bool | None = None,
) -> None:
    """Emit bounded schema-dispatch metadata without reflecting the frame.

    Parameters
    ----------
    resource : WebSocketResource
        The resource handling the frame; only its type name is recorded.
    outcome : str
        The dispatch decision, coerced to ``other`` when unrecognised.
    handler_mode : str | None, optional
        ``"registered"`` or ``"conventional"`` once a handler was selected.
    strict : bool | None, optional
        The selected handler's strictness, once known.
    """
    logger.debug(
        "Schema dispatch observation",
        extra={
            "operation": "schema_dispatch",
            "outcome": (outcome if outcome in _SCHEMA_DISPATCH_OUTCOMES else "other"),
            "resource_type": _type_name(resource),
            "handler_mode": handler_mode,
            "strict": strict,
        },
    )


class Envelope(ms.Struct, frozen=True):
    """Envelope where omitted and null payloads both decode to ``None``."""

    type: str
    payload: typ.Any | None = None


@dc.dataclass(slots=True, repr=False)
class HandlerInvocationContext:
    """Context for invoking a message handler.

    Attributes
    ----------
    target_type : type | None
        Struct selected by schema dispatch, when it differs from the handler's
        annotation. Schema mode always converts to the resolved member so an
        unannotated conventional handler still receives a typed payload.
    monotonic_clock : cabc.Callable[[], float]
        Clock used to measure typed-payload rejection time. Defaults to
        :func:`time.monotonic`.
    """

    resource: WebSocketResource
    ws: WebSocketLike
    raw: str | bytes
    handler_info: HandlerInfo
    payload: object
    target_type: type | None = None
    monotonic_clock: cabc.Callable[[], float] = time.monotonic

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
    ``on_session_start``, ``on_disconnect`` or ``on_unhandled``, nor an alias of
    one. A class without the attribute (a non-monitored stand-in) offers no
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
    """Convert the retained payload, or re-decode a schema frame from the wire."""
    # Schema dispatch decodes frames generically so undeclared keys can be
    # inspected. That mapping is inspection-only: integer mapping keys arrive as
    # strings, ``msgspec.Raw`` fields arrive as decoded JSON, and neither the
    # member's declared field types nor the handler's strictness are applied.
    # Inspection floats do decode through ``decimal.Decimal``, so their values
    # stay exact, but the mapping is still not the declared payload. Re-decoding
    # the original frame into the resolved member restores msgspec's wire
    # semantics.
    #
    # The wire decode carries the handler's strictness rather than falling back
    # to converting the mapping. A fallback would let one field that needs lax
    # coercion change how its neighbours decode, losing the wire values the
    # second decode exists to preserve.
    if context.target_type is None:
        return ms.convert(
            context.payload,
            type=payload_type,
            strict=context.handler_info.strict,
        )
    return msjson.decode(
        context.raw,
        type=payload_type,
        strict=context.handler_info.strict,
    )


def _log_envelope_rejection(
    context: HandlerInvocationContext,
    exc: ms.ValidationError,
    started: float,
) -> None:
    """Record a rejected envelope conversion with bounded metadata.

    Envelope dispatch has no schema observation of its own, so this record is
    what lets application telemetry count rejected conversions and their
    elapsed time. Schema dispatch already records its decision through
    ``_log_schema_dispatch``, so it does not emit this record as well.

    Parameters
    ----------
    context : HandlerInvocationContext
        The invocation being rejected; only its type labels are recorded.
    exc : ms.ValidationError
        The vendor error whose type is recorded instead of its message.
    started : float
        Monotonic reading taken before validation began.
    """
    if context.target_type is not None:
        return
    logger.debug(
        "Operation observation",
        extra={
            "operation": "dispatch",
            "outcome": "rejected",
            "duration_seconds": max(0.0, context.monotonic_clock() - started),
            "exception_type": _type_name(exc),
            "expected_type": _expected_type_label(context.handler_info),
        },
    )


async def convert_and_invoke_handler(
    context: HandlerInvocationContext,
) -> _DeliveryOutcome:
    """Validate typed payloads, including omitted and null values, then invoke.

    Omission and null are equivalent; handler parameter defaults are not applied.

    Returns
    -------
    _DeliveryOutcome
        ``"delivered"`` when the handler ran, otherwise the rejection that
        routed the frame to ``on_unhandled``.
    """
    payload_type = (
        context.target_type
        if context.target_type is not None
        else context.handler_info.payload_type
    )
    payload = context.payload
    failure: _DeliveryOutcome | None = None
    if payload_type is not None:
        started = context.monotonic_clock()
        try:
            validate_strict_payload(
                payload, payload_type, strict=context.handler_info.strict
            )
        except ms.ValidationError as exc:
            failure = "rejected_fields"
            _log_envelope_rejection(context, exc, started)
        else:
            try:
                payload = _convert_payload(context, payload_type)
            except ms.ValidationError as exc:
                failure = "conversion_failed"
                _log_envelope_rejection(context, exc, started)
    if failure is not None:
        # The fallback runs after the ``except`` blocks, not inside them, so an
        # exception it raises does not inherit the vendor ``ValidationError``
        # as its implicit ``__context__``.
        await context.resource.on_unhandled(context.ws, context.raw)
        return failure
    await context.handler_info.handler(context.resource, context.ws, payload)
    return "delivered"


async def dispatch(
    resource: WebSocketResource, ws: WebSocketLike, raw: str | bytes
) -> None:
    """Dispatch ``raw`` to a handler based on :attr:`resource.schema`."""
    if resource.schema is not None:
        await dispatch_with_schema(resource, ws, raw)
    else:
        await dispatch_with_envelope(resource, ws, raw)


def _decode_schema_frame(raw: str | bytes) -> dict[str, object] | None:
    """Decode ``raw`` to a JSON object, or return ``None`` when it is not one."""
    # Returning from inside the ``except`` suite keeps the vendor decode error
    # out of ``__context__``: callers fall back outside it, so no application
    # error is raised while that exception is being handled. The decode error
    # can quote the frame, so it must not be chained to a reply.
    try:
        payload = _INSPECTION_DECODER.decode(raw)
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

    Raises
    ------
    TypeError
        If ``resource.schema`` is not a valid tagged-Struct schema.
    """
    # ``dispatch`` only routes here when a schema is set. Resolve the tag table
    # first so a misconfigured schema raises for every frame, good or bad,
    # rather than surfacing only once a well-formed object arrives.
    schema = typ.cast("type", resource.schema)
    try:
        table = schema_dispatch(schema)
    except TypeError:
        # A configuration error is not a bad frame: record it and let it
        # propagate so the broken schema is not hidden behind on_unhandled.
        _log_schema_dispatch(resource, "configuration_error")
        raise
    payload = _decode_schema_frame(raw)
    if payload is None:
        _log_schema_dispatch(resource, "malformed")
        await resource.on_unhandled(ws, raw)
        return

    resolution = table.resolve(payload)
    if resolution is None:
        _log_schema_dispatch(resource, "unknown_tag")
        await resource.on_unhandled(ws, raw)
        return
    member, tag = resolution

    handler_mode = "registered"
    entry = resource.__class__._struct_handlers.get(member)
    if entry is None:
        # msgspec permits integer tags; conventional lookup uses strings.
        handler_mode = "conventional"
        entry = find_conventional_handler(resource, str(tag))
    if entry is None:
        _log_schema_dispatch(resource, "no_handler", handler_mode=handler_mode)
        await resource.on_unhandled(ws, raw)
        return

    ctx = HandlerInvocationContext(resource, ws, raw, entry, payload, member)
    outcome = await convert_and_invoke_handler(ctx)
    _log_schema_dispatch(
        resource,
        outcome,
        handler_mode=handler_mode,
        strict=entry.strict,
    )


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
