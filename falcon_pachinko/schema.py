"""Schema validation helpers."""

from __future__ import annotations

import inspect
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


def _require_struct_tag(struct: type[ms.Struct]) -> None:
    """Ensure ``struct`` declares the tag schema dispatch relies upon."""
    info = msinspect.type_info(struct)
    if not isinstance(info, msinspect.StructType) or info.tag is None:
        msg = "schema Struct types must define a tag"
        raise TypeError(msg)


def _require_uniform_tag_kinds(structs: cabc.Sequence[type[ms.Struct]]) -> None:
    """Ensure ``structs`` agree on a single kind of dispatch tag.

    msgspec accepts both integer and string tags, but rejects a union that
    contains both kinds. It does so only when the union is first derived at
    decode time, so an invalid schema would otherwise pass class creation and
    then break the receive loop on the first message. Catching it here reports
    the mistake where it was made. A lone Struct has nothing to disagree with,
    and ``msgspec`` already rejects any tag that is neither an ``int`` nor a
    ``str``.

    Raises
    ------
    TypeError
        If the members do not all use the same kind of dispatch tag.
    """
    kinds = {
        type(info.tag)
        for struct in structs
        if isinstance(info := msinspect.type_info(struct), msinspect.StructType)
    }
    if len(kinds) > 1:
        names = [_class_name(struct) for struct in structs]
        msg = f"schema tags must all be strings or all be integers: {names!r}"
        raise TypeError(msg)


def validate_schema_types(schema: type) -> None:
    """Ensure all schema types are :class:`msgspec.Struct` with tags."""
    types = typ.get_args(schema) or (schema,)
    structs: list[type[ms.Struct]] = []
    for t in types:
        # Narrow before inspecting metadata: a non-struct member must fail on
        # the membership check rather than inside ``msgspec.inspect``.
        struct = _require_struct_type(t)
        _require_struct_tag(struct)
        structs.append(struct)
    _require_uniform_tag_kinds(structs)


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


def validate_strict_payload(
    payload: object, payload_type: type, *, strict: bool
) -> None:
    """Raise if ``payload`` contains unknown fields in strict mode."""
    if not requires_strict_validation(payload, payload_type, strict=strict):
        return
    info = msinspect.type_info(payload_type)
    if isinstance(info, msinspect.StructType) and (
        extra := set(payload) - {f.name for f in info.fields}
    ):
        raise_unknown_fields(extra, expected_type=payload_type)
