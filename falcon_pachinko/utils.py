"""Helper utilities for message validation and naming."""

from __future__ import annotations

import re
import typing as typ

import msgspec as ms

from .diagnostics import DiagnosticSanitizer, _class_name, _identifiers


def duplicate_payload_type_msg(
    payload_type: type, handler_name: str | None = None
) -> str:
    """Return a detailed error message for duplicate payload types."""
    msg = f"Duplicate payload type in handlers: {payload_type!r}"
    if handler_name:
        msg += f" (handler: {handler_name})"
    return msg


class _UnknownFieldOptions(typ.TypedDict, total=False):
    """Optional schema and sample configuration for validation diagnostics."""

    expected_type: type | None
    sanitizer: DiagnosticSanitizer | None


def raise_unknown_fields(
    extra_fields: set[str],
    payload: dict | None = None,
    *,
    include_payload: bool = False,
    **options: typ.Unpack[_UnknownFieldOptions],
) -> None:
    """Raise a structural validation error, optionally adding a sanitized sample."""
    unknown_options = options.keys() - {"expected_type", "sanitizer"}
    if unknown_options:
        msg = f"Unknown diagnostic options: {_identifiers(unknown_options)}"
        raise TypeError(msg)
    expected_type = options.get("expected_type")
    sanitizer = options.get("sanitizer")
    details = f"Unknown fields in payload: {_identifiers(extra_fields)}"
    if expected_type is not None:
        details += f" (expected: {_class_name(expected_type)})"
    if include_payload and payload is not None:
        formatter = sanitizer if sanitizer is not None else DiagnosticSanitizer()
        details += f" -> {formatter.format_sample(payload)}"
    raise ms.ValidationError(details)


def to_snake_case(name: str) -> str:
    """Best-effort conversion of ``name`` to ``snake_case``."""
    name = re.sub(r"[^0-9a-zA-Z]+", "_", name)
    name = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", name)
    name = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", name)
    return name.lower()
