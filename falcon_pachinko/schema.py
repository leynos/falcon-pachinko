"""Schema validation helpers."""

from __future__ import annotations

import dataclasses as dc
import functools
import inspect
import types
import typing as typ

import msgspec as ms
import msgspec.inspect as msinspect

from .diagnostics import _class_name
from .utils import duplicate_payload_type_msg, raise_unknown_fields

if typ.TYPE_CHECKING:  # pragma: no cover - used for type hints
    import collections.abc as cabc

    from .handlers import HandlerInfo
    from .resource import WebSocketResource


def _require_struct_type(candidate: object) -> type[ms.Struct]:
    """Return ``candidate`` narrowed to a :class:`msgspec.Struct` subclass."""
    if not (inspect.isclass(candidate) and issubclass(candidate, ms.Struct)):
        msg = "schema must contain only msgspec.Struct types"
        raise TypeError(msg)
    return candidate


def _require_tagged_struct(struct: type[ms.Struct]) -> msinspect.StructType:
    """Ensure ``struct`` declares the tag schema dispatch relies upon.

    Returns
    -------
    msinspect.StructType
        The inspected Struct, already known to be tagged and object-encoded.

    Raises
    ------
    TypeError
        If ``struct`` is not tagged, or if it is encoded as a JSON array.
    """
    info = msinspect.type_info(struct)
    if not isinstance(info, msinspect.StructType) or info.tag is None:
        msg = "schema Struct types must define a tag"
        raise TypeError(msg)
    if info.array_like:
        # An array-encoded Struct has no keyed tag on the wire, so the
        # generic decode below could never resolve it. Reject the schema
        # rather than installing a member that is unreachable by design.
        msg = "schema Struct types must not use array_like encoding"
        raise TypeError(msg)
    return info


def validate_schema_types(schema: type) -> None:
    """Ensure all schema types are tagged, object-encoded Structs.

    Parameters
    ----------
    schema : type
        A tagged :class:`msgspec.Struct`, or a union of them.

    Raises
    ------
    TypeError
        If a member is not a tagged Struct, uses array encoding, or if the
        members do not all use the same kind of tag.
    """
    types = typ.get_args(schema) or (schema,)
    kinds: set[type] = set()
    for t in types:
        # Narrow before inspecting metadata: a non-struct member must fail on
        # the membership check rather than inside ``msgspec.inspect``.
        struct = _require_struct_type(t)
        kinds.add(type(_require_tagged_struct(struct).tag))
    # msgspec accepts both integer and string tags, but rejects a union mixing
    # them -- and only when it first derives that union at decode time. Left
    # unchecked the schema would pass class creation and then break the receive
    # loop on the first message, so report the mistake where it was made. A
    # lone Struct has nothing to disagree with, and msgspec already rejects any
    # tag that is neither an ``int`` nor a ``str``.
    if len(kinds) > 1:
        names = [_class_name(struct) for struct in types]
        msg = f"schema tags must all be strings or all be integers: {names!r}"
        raise TypeError(msg)


@dc.dataclass(frozen=True, slots=True)
class SchemaDispatch:
    """Tag metadata for a schema-backed resource.

    A schema is a tagged Struct or a union of them. Schema dispatch decodes a
    frame to a plain mapping first, so undeclared fields are still present when
    the strict decision is made, and then uses this table to select the member
    that the frame's tag names.
    """

    tag_field: str
    tag_kind: type
    members: cabc.Mapping[object, type[ms.Struct]]

    def resolve(
        self, payload: cabc.Mapping[str, object]
    ) -> tuple[type[ms.Struct], object] | None:
        """Return the member and tag selected by ``payload``, or ``None``.

        Parameters
        ----------
        payload : collections.abc.Mapping
            The generically decoded JSON object, whose undeclared fields are
            still present.

        Returns
        -------
        tuple[type[msgspec.Struct], object] | None
            The selected Struct and its tag, or ``None`` when the frame has no
            usable tag or names no member.
        """
        if self.tag_field not in payload:
            return None
        tag = payload[self.tag_field]
        # ``bool`` subclasses ``int``, so an identity check is what keeps JSON
        # ``true`` from selecting the integer tag ``1``. It also rejects any
        # other tag kind the union did not declare.
        if type(tag) is not self.tag_kind:
            return None
        member = self.members.get(tag)
        if member is None:
            return None
        return member, tag


@functools.cache
def schema_dispatch(schema: type) -> SchemaDispatch:
    """Resolve and cache the tag table for ``schema``.

    Class creation validates a declared ``schema``, but assigning one at
    runtime bypasses that check, so the resolver validates again. A mistake is
    a configuration error: it raises ``TypeError`` rather than sending every
    frame to ``on_unhandled``, which would hide the broken schema.

    Parameters
    ----------
    schema : type
        A tagged :class:`msgspec.Struct`, or a union of them.

    Returns
    -------
    SchemaDispatch
        The shared tag field, the declared tag kind, and each tag's member.

    Raises
    ------
    TypeError
        If ``schema`` is not a valid tagged-Struct schema, or mixes tag kinds.
    """
    # ``type_info`` derives the union itself, which is what reports msgspec's
    # mixed-tag and mismatched-tag-field diagnostics for a schema installed
    # after class creation.
    info = msinspect.type_info(schema)
    validate_schema_types(schema)
    if isinstance(info, msinspect.UnionType):
        members_info = typ.cast("tuple[msinspect.StructType, ...]", info.types)
    else:
        members_info = (typ.cast("msinspect.StructType", info),)
    tag_field = members_info[0].tag_field
    if tag_field is None:  # pragma: no cover - validate_schema_types guarantees a tag
        msg = "schema Struct types must define a tag"
        raise TypeError(msg)
    members: dict[object, type[ms.Struct]] = {}
    for member_info in members_info:
        tag = member_info.tag
        if tag is None:  # pragma: no cover - validate_schema_types guarantees a tag
            msg = "schema Struct types must define a tag"
            raise TypeError(msg)
        members[tag] = member_info.cls
    return SchemaDispatch(
        tag_field=tag_field,
        tag_kind=type(members_info[0].tag),
        # The dispatch table is shared by every resource using this schema, so
        # expose it read-only: mutating one caller's view would corrupt
        # dispatch for all of them.
        members=types.MappingProxyType(members),
    )


def populate_struct_handlers(cls: type[WebSocketResource]) -> dict[type, HandlerInfo]:
    """Create mapping of struct types to handlers for ``cls``."""
    mapping: dict[type, HandlerInfo] = {}
    for info in cls.handlers.values():
        handler = info.handler
        payload_type = info.payload_type
        if payload_type is None or not issubclass(payload_type, ms.Struct):
            continue
        existing = mapping.get(payload_type)
        if existing is not None:
            handler_name: str = getattr(handler, "__qualname__", repr(handler))
            raise ValueError(duplicate_payload_type_msg(payload_type, handler_name))
        mapping[payload_type] = info
    return mapping


def requires_strict_validation(
    payload: object, payload_type: type, *, strict: bool
) -> typ.TypeGuard[dict[str, typ.Any]]:
    """Return ``True`` when ``payload`` needs strict validation."""
    return strict and isinstance(payload, dict) and issubclass(payload_type, ms.Struct)


@functools.cache
def _allowed_wire_keys(payload_type: type) -> frozenset[str] | None:
    """Return the keys a strict frame may declare for ``payload_type``.

    ``msinspect.type_info`` walks the whole type graph, and schema mode now
    validates every strict frame, so the key set is computed once per Struct
    rather than once per message.

    Parameters
    ----------
    payload_type : type
        The declared type of a handler's payload.

    Returns
    -------
    frozenset[str] | None
        The encoded wire names plus the tag field, or ``None`` when
        ``payload_type`` is not a Struct and therefore has no such contract.
    """
    info = msinspect.type_info(payload_type)
    if not isinstance(info, msinspect.StructType):
        return None
    # Compare wire names, not Python attribute names: ``rename`` changes what
    # a valid frame may contain. The tag field travels beside the fields, so it
    # is a declared key too.
    allowed = {field.encode_name for field in info.fields}
    if info.tag_field is not None:
        allowed.add(info.tag_field)
    return frozenset(allowed)


def validate_strict_payload(
    payload: object, payload_type: type, *, strict: bool
) -> None:
    """Raise if ``payload`` contains unknown fields in strict mode.

    The check is shallow: only the top-level mapping is compared with the
    target Struct's declared wire names. Whether a nested Struct rejects its
    own extra fields is that Struct's ``forbid_unknown_fields`` decision, which
    ``msgspec`` enforces during conversion regardless of ``strict``.

    Parameters
    ----------
    payload : object
        The decoded payload. Only a mapping can carry undeclared keys.
    payload_type : type
        The Struct the payload will be converted into.
    strict : bool
        Whether the handler asked for strict validation.

    Raises
    ------
    ValidationError
        If the payload declares a key that is neither a field's encoded wire
        name nor the Struct's tag field.
    """  # ruff: ignore[docstring-extraneous-exception] - raised by raise_unknown_fields
    if not requires_strict_validation(payload, payload_type, strict=strict):
        return
    allowed = _allowed_wire_keys(payload_type)
    if allowed is None:
        return
    if extra := set(payload) - allowed:
        raise_unknown_fields(extra, expected_type=payload_type)
