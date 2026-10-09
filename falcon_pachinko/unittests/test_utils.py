"""Tests for public validation helpers."""

from __future__ import annotations

import msgspec as ms
import pytest

from falcon_pachinko import utils


def test_validation_error_is_msgspec_validation_error() -> None:
    """The public alias retains msgspec's exception identity."""
    assert utils.ValidationError is ms.ValidationError, (
        "the public alias must preserve msgspec exception identity"
    )


def test_validation_error_is_decode_error() -> None:
    """The public alias retains msgspec's decode error hierarchy."""
    assert issubclass(utils.ValidationError, ms.DecodeError), (
        "the public alias must preserve the decode error hierarchy"
    )


def test_validation_error_is_usable_in_type_annotations() -> None:
    """Callers can use the public exception name as an annotation."""

    def handle(error: utils.ValidationError) -> str:
        return str(error)

    assert handle(ms.ValidationError("invalid payload")) == "invalid payload", (
        "the public alias must be usable in exception annotations"
    )


def test_raise_unknown_fields_uses_sorted_names() -> None:
    """Unknown field names are sorted in the raised error message."""
    with pytest.raises(
        utils.ValidationError,
        match=r"Unknown fields in payload: \['a', 'b'\]",
    ):
        utils.raise_unknown_fields({"b", "a"})


def test_msgspec_validation_error_catches_unknown_fields() -> None:
    """Existing msgspec exception handlers continue to catch the error."""
    with pytest.raises(ms.ValidationError):
        utils.raise_unknown_fields({"b", "a"})
