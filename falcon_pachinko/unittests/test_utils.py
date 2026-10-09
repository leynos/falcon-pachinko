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
        "The public alias must preserve the decode error hierarchy."
    )


def test_validation_error_remains_a_decode_error() -> None:
    """The raised error remains catchable as a msgspec DecodeError."""
    with pytest.raises(ms.DecodeError) as error:
        utils.raise_unknown_fields({"a"})

    assert isinstance(error.value, ms.ValidationError), (
        "The DecodeError must also be a msgspec ValidationError."
    )


def test_validation_error_is_listed_in_public_exports() -> None:
    """ValidationError is part of the utility module's explicit API."""
    assert "ValidationError" in utils.__all__, (
        "ValidationError must be listed in utils.__all__."
    )


def test_validation_error_is_usable_in_type_annotations() -> None:
    """Callers can use the public exception name as an annotation."""

    def handle(error: utils.ValidationError) -> str:
        return str(error)

    assert handle(ms.ValidationError("invalid payload")) == "invalid payload", (
        "the public alias must be usable in exception annotations"
    )


def test_raise_unknown_fields_uses_sorted_names() -> None:
    """Unknown field names stay sorted in the raised error message."""
    with pytest.raises(utils.ValidationError) as error:
        utils.raise_unknown_fields({"b", "a"})

    assert str(error.value) == "Unknown fields in payload: ['a', 'b']", (
        "Unknown field names must be sorted in the existing message."
    )


def test_raise_unknown_fields_omits_payload_by_default() -> None:
    """Payload data stays out of validation errors unless requested."""
    with pytest.raises(ms.ValidationError) as error:
        utils.raise_unknown_fields({"a"}, payload={"secret": "value"})

    assert " -> " not in str(error.value), (
        "The payload separator must be absent unless payload inclusion is enabled."
    )
    assert "secret" not in str(error.value), (
        "Payload contents must be omitted by default."
    )


def test_raise_unknown_fields_truncates_included_payload() -> None:
    """Included payload snippets are bounded and end with an ellipsis."""
    with pytest.raises(ms.ValidationError) as error:
        utils.raise_unknown_fields(
            {"a"}, payload={"value": "x" * 260}, include_payload=True
        )

    _, separator, snippet = str(error.value).partition(" -> ")
    assert separator == " -> ", (
        "Included payload text must follow the documented separator."
    )
    assert len(snippet) == 200, (
        "The included payload snippet must be 200 characters."
    )
    assert snippet.endswith("..."), (
        "A truncated payload snippet must end in an ellipsis."
    )


def test_msgspec_validation_error_catches_unknown_fields() -> None:
    """Existing msgspec exception handlers continue to catch the error."""
    with pytest.raises(ms.ValidationError):
        utils.raise_unknown_fields({"b", "a"})
