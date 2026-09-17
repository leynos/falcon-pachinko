"""Schema validation helpers."""

from __future__ import annotations

import inspect
import typing as typ

import msgspec as ms
import msgspec.inspect as msinspect

from .utils import duplicate_payload_type_msg, raise_unknown_fields

if typ.TYPE_CHECKING:  # pragma: no cover - used for type hints
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


def validate_schema_types(schema: type) -> None:
    """Ensure all schema types are :class:`msgspec.Struct` with tags."""
    types = typ.get_args(schema) or (schema,)
    for t in types:
        # Narrow before inspecting metadata: a non-struct member must fail on
        # the membership check rather than inside ``msgspec.inspect``.
        struct = _require_struct_type(t)
        _require_struct_tag(struct)


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
        raise_unknown_fields(extra)
