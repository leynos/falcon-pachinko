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


def validate_schema_types(schema: type) -> None:
    """Ensure all schema types are :class:`msgspec.Struct` with tags."""
    types = typ.get_args(schema) or (schema,)
    for t in types:
        if not (inspect.isclass(t) and issubclass(t, ms.Struct)):
            raise TypeError("schema must contain only msgspec.Struct types")  # noqa: TRY003

        info = msinspect.type_info(t)
        if typ.cast("msinspect.StructType", info).tag is None:
            raise TypeError("schema Struct types must define a tag")  # noqa: TRY003


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
            raise ValueError(
                duplicate_payload_type_msg(payload_type, handler.__qualname__)
            )
        mapping[payload_type] = info
    return mapping


def requires_strict_validation(
    payload: object, payload_type: type, *, strict: bool
) -> bool:
    """Return ``True`` when ``payload`` needs strict validation."""
    return strict and isinstance(payload, dict) and issubclass(payload_type, ms.Struct)


def validate_strict_payload(
    payload: object, payload_type: type, *, strict: bool
) -> None:
    """Raise if ``payload`` contains unknown fields in strict mode."""
    if requires_strict_validation(payload, payload_type, strict=strict):
        info = msinspect.type_info(payload_type)
        allowed = {f.name for f in typ.cast("msinspect.StructType", info).fields}
        if extra := set(typ.cast("dict[str, typ.Any]", payload)) - allowed:
            raise_unknown_fields(extra)
