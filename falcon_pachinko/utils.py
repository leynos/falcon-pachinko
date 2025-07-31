"""Helper utilities for message validation and naming."""

from __future__ import annotations

import re

import msgspec


def duplicate_payload_type_msg(
    payload_type: type, handler_name: str | None = None
) -> str:
    """Return a detailed error message for duplicate payload types."""
    msg = f"Duplicate payload type in handlers: {payload_type!r}"
    if handler_name:
        msg += f" (handler: {handler_name})"
    return msg


def raise_unknown_fields(
    extra_fields: set[str],
    payload: dict | None = None,
    *,
    include_payload: bool = False,
) -> None:
    """Raise a validation error for unknown fields."""
    details = f"Unknown fields in payload: {sorted(extra_fields)}"
    if include_payload and payload is not None:
        snippet = str(payload)
        if len(snippet) > 200:
            snippet = f"{snippet[:197]}..."
        details += f" -> {snippet}"
    raise msgspec.ValidationError(details)


def to_snake_case(name: str) -> str:
    """Best-effort conversion of ``name`` to ``snake_case``."""
    name = re.sub(r"[^0-9a-zA-Z]+", "_", name)
    name = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", name)
    name = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", name)
    return name.lower()
