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


def _require_tagged_struct(candidate: object) -> msinspect.StructType:
    """Ensure ``candidate`` is a tagged, object-encoded Struct."""
    # Narrow before inspecting metadata: a non-struct member must fail on the
    # membership check rather than inside ``msgspec.inspect``.
    struct = _require_struct_type(candidate)
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
    # Named ``members`` rather than ``types`` so the module import stays
    # visible in this scope.
    members = typ.get_args(schema) or (schema,)
    infos = [_require_tagged_struct(member) for member in members]
    kinds = {type(info.tag) for info in infos}
    # msgspec accepts both integer and string tags, but rejects a union mixing
    # them -- and only when it first derives that union at decode time. Left
    # unchecked the schema would pass class creation and then break the receive
    # loop on the first message, so report the mistake where it was made. A
    # lone Struct has nothing to disagree with, and msgspec already rejects any
    # tag that is neither an ``int`` nor a ``str``.
    if len(kinds) > 1:
        names = [_class_name(struct) for struct in members]
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
        tag = payload.get(self.tag_field)
        # ``bool`` subclasses ``int``, so an identity check is what keeps JSON
        # ``true`` from selecting the integer tag ``1``. It also rejects any
        # other tag kind the union did not declare, and keeps an unhashable
        # payload value away from ``members.get``.
        member = self.members.get(tag) if type(tag) is self.tag_kind else None
        if member is None:
            return None
        return member, tag


def _schema_member_infos(schema: type) -> tuple[msinspect.StructType, ...]:
    """Return the inspected Struct members of ``schema`` in declaration order."""
    # ``type_info`` derives the union itself, which is what reports msgspec's
    # mixed-tag and mismatched-tag-field diagnostics for a schema installed
    # after class creation.
    info = msinspect.type_info(schema)
    validate_schema_types(schema)
    match info:
        case msinspect.UnionType(types=members):
            return typ.cast("tuple[msinspect.StructType, ...]", members)
        case _:
            return (typ.cast("msinspect.StructType", info),)


def _tagged_member(member_info: msinspect.StructType) -> tuple[object, type[ms.Struct]]:
    """Return ``member_info``'s declared tag and Struct class."""
    # ``validate_schema_types`` has already rejected untagged members, so the
    # tag is present.
    return typ.cast("object", member_info.tag), member_info.cls


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
    """  # ruff: ignore[docstring-extraneous-exception] - raised by the helpers
    members_info = _schema_member_infos(schema)
    # ``validate_schema_types`` guarantees every member is tagged, so msgspec
    # supplies a tag field name as well.
    tag_field = typ.cast("str", members_info[0].tag_field)
    members = dict(map(_tagged_member, members_info))
    return SchemaDispatch(
        tag_field=tag_field,
        tag_kind=type(members_info[0].tag),
        # The dispatch table is shared by every resource using this schema, so
        # expose it read-only: mutating one caller's view would corrupt
        # dispatch for all of them.
        members=types.MappingProxyType(members),
    )


def _register_struct_handler(
    mapping: dict[type, HandlerInfo], info: HandlerInfo
) -> None:
    """Map ``info``'s payload Struct, rejecting a duplicate registration."""
    payload_type = info.payload_type
    if payload_type is None or not issubclass(payload_type, ms.Struct):
        return
    if mapping.get(payload_type) is not None:
        handler = info.handler
        handler_name: str = getattr(handler, "__qualname__", repr(handler))
        msg = duplicate_payload_type_msg(payload_type, handler_name)
        raise ValueError(msg)
    mapping[payload_type] = info


def populate_struct_handlers(cls: type[WebSocketResource]) -> dict[type, HandlerInfo]:
    """Create mapping of struct types to handlers for ``cls``."""
    mapping: dict[type, HandlerInfo] = {}
    for info in cls.handlers.values():
        _register_struct_handler(mapping, info)
    return mapping


def requires_strict_validation(
    payload: object, payload_type: type, *, strict: bool
) -> typ.TypeGuard[dict[str, typ.Any]]:
    """Return ``True`` when ``payload`` needs strict validation."""
    return strict and isinstance(payload, dict) and issubclass(payload_type, ms.Struct)


@functools.cache
def _allowed_wire_keys(payload_type: type) -> frozenset[str]:
    """Return the keys a strict frame may declare for ``payload_type``."""
    # ``msinspect.type_info`` walks the whole type graph, and schema mode now
    # validates every strict frame, so the key set is computed once per Struct
    # rather than once per message. A non-Struct has no such contract.
    info = msinspect.type_info(payload_type)
    if not isinstance(info, msinspect.StructType):
        return frozenset()
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
    if extra := set(payload) - _allowed_wire_keys(payload_type):
        raise_unknown_fields(extra, expected_type=payload_type)
