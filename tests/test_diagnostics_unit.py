"""Bounded diagnostics must never invoke application display methods."""

from __future__ import annotations

import dataclasses as dc
import typing as typ

import pytest
from hypothesis import given
from hypothesis import strategies as st

from falcon_pachinko import DEFAULT_SENSITIVE_KEYS, DiagnosticSanitizer
from falcon_pachinko.diagnostics import (
    _frame_metadata,
    _identifier,
    _identifiers,
    _safe_scalar,
    _type_name,
)
from falcon_pachinko.hooks import HookEvent

CANARY = "CANARY-local-auth-74b1"


class Hostile:
    """Catch accidental reflection of caller-controlled objects."""

    def __repr__(self) -> str:
        """Reject representation calls."""
        msg = "repr invoked"
        raise AssertionError(msg)

    def __str__(self) -> str:
        """Reject string conversion calls."""
        msg = "str invoked"
        raise AssertionError(msg)

    def __len__(self) -> int:
        """Reject length calls."""
        msg = "len invoked"
        raise AssertionError(msg)


class HostileDict(dict):  # ruff: ignore[subclass-builtin]  # deliberately test dict-subclass rejection
    """A mapping subclass must not be traversed."""

    def items(self) -> typ.Never:
        """Reject traversal."""
        msg = "items invoked"
        raise AssertionError(msg)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("abc", ("text", 3)),
        (b"abc", ("bytes", 3)),
        (None, ("none", None)),
        (Hostile(), ("object", None)),
    ],
)
def test_frame_metadata(value: object, expected: tuple[str, int | None]) -> None:
    """Only exact built-in frames disclose length."""
    assert _frame_metadata(value) == expected, (
        "the diagnostic safety contract must hold"
    )


def test_metadata_omits_hostile_and_oversized_values() -> None:
    """Metadata never converts caller objects or long integers."""
    assert _type_name(Hostile()) == "Hostile", (
        "the diagnostic safety contract must hold"
    )
    assert _identifier(Hostile()) == "<invalid>", (
        "the diagnostic safety contract must hold"
    )
    assert _identifier("x" * 1000) == "<long>", (
        "the diagnostic safety contract must hold"
    )
    assert _identifier("bad\nname") == "<invalid>", (
        "the diagnostic safety contract must hold"
    )
    assert _safe_scalar(Hostile()) == "<omitted>", (
        "the diagnostic safety contract must hold"
    )
    assert _safe_scalar(10**1000) == "<integer>", (
        "the diagnostic safety contract must hold"
    )
    assert _safe_scalar(HookEvent.AFTER_RECEIVE) == "after_receive", (
        "the diagnostic safety contract must hold"
    )
    assert _safe_scalar(CANARY) == "<omitted>", (
        "the diagnostic safety contract must hold"
    )
    assert _identifiers({"z", "a"}) == '["a", "z"]', (
        "the diagnostic safety contract must hold"
    )
    assert len(_identifiers({f"field{i}" for i in range(1000)})) < 1500, (
        "the diagnostic safety contract must hold"
    )


@pytest.mark.parametrize(
    "key",
    [
        *sorted(DEFAULT_SENSITIVE_KEYS),
        "AUTHORIZATION",
        "x-access.Token",
        "Refresh_Token",
        "Api-Key",
        "pass word",
        "clientCredential",
        "session_cookie",
        "private.secret.value",
    ],
)
def test_sensitive_fragments_are_redacted_recursively(key: str) -> None:
    """Variants remain redacted inside sequences and mappings."""
    sample = {"outer": [{key: CANARY}, ({key: CANARY},)]}
    rendered = DiagnosticSanitizer().format_sample(sample)
    assert CANARY not in rendered, "the diagnostic safety contract must hold"
    assert "<redacted>" in rendered, "the diagnostic safety contract must hold"


def test_sanitizer_preserves_safe_sample_and_extends_keys() -> None:
    """Opt-in output can include ordinary values and custom redaction."""
    sanitizer = DiagnosticSanitizer(extra_sensitive_keys=frozenset({"Tenant-Key"}))
    assert sanitizer.sanitize({"ok": "visible", "tenant_key": CANARY}) == {
        "ok": "visible",
        "tenant_key": "<redacted>",
    }, "the diagnostic safety contract must hold"
    assert "visible" in sanitizer.format_sample({"ok": "visible"}), (
        "the diagnostic safety contract must hold"
    )
    field = "max_depth"
    with pytest.raises(dc.FrozenInstanceError):
        setattr(sanitizer, field, 1)


def test_sanitizer_omits_unsupported_keys_bytes_and_cycles() -> None:
    """Uncertain values and binary frames never invoke decoding or display."""
    # pylint: disable=prefer-snapshot-substring  # canary and traversal predicates are independent of sample formatting
    sample: list[object] = [Hostile(), HostileDict(token=CANARY), CANARY.encode()]
    sample.append(sample)
    rendered = DiagnosticSanitizer().format_sample({"items": sample, 1: CANARY})
    assert CANARY not in rendered, "the diagnostic safety contract must hold"
    assert "<cycle>" in rendered, "the diagnostic safety contract must hold"
    assert "<bytes len=" in rendered, "the diagnostic safety contract must hold"
    assert "Hostile" in rendered, "the diagnostic safety contract must hold"


def test_sanitizer_bounds_each_dimension() -> None:
    """Every container, scalar, and output budget is independently bounded."""
    sanitizer = DiagnosticSanitizer(max_depth=1, max_items=2, max_string_length=4)
    assert sanitizer.sanitize([[[CANARY]]]) == ["<depth>"], (
        "the diagnostic safety contract must hold"
    )
    assert sanitizer.sanitize("abcdef") == "abcd", (
        "the diagnostic safety contract must hold"
    )
    assert sanitizer.sanitize(10**1000) == "<integer>", (
        "the diagnostic safety contract must hold"
    )
    sample = sanitizer.sanitize(list(range(100)))
    assert isinstance(sample, list), "the diagnostic safety contract must hold"
    assert len(sample) == 3, "the diagnostic safety contract must hold"
    assert len(sanitizer.format_sample({"x": "abc" * 10000})) < 100, (
        "the diagnostic safety contract must hold"
    )


@pytest.mark.parametrize(
    "option", ["max_depth", "max_items", "max_string_length", "max_output_length"]
)
def test_invalid_bounds_fail_closed(option: str) -> None:
    """Negative or excessively large limits are configuration errors."""
    with pytest.raises(ValueError, match="bound"):
        dc.replace(DiagnosticSanitizer(), **{option: -1})


_RECURSIVE = st.recursive(
    st.none() | st.booleans() | st.integers() | st.text() | st.binary(),
    lambda children: (
        st.lists(children, max_size=5)
        | st.dictionaries(st.text(max_size=20), children, max_size=5)
    ),
    max_leaves=30,
)


@given(value=_RECURSIVE, budget=st.integers(min_value=0, max_value=200))
def test_recursive_samples_obey_total_budget(value: object, budget: int) -> None:
    """Output remains bounded for arbitrary nested built-in data."""
    assert (
        len(DiagnosticSanitizer(max_output_length=budget).format_sample(value))
        <= budget
    ), "the diagnostic safety contract must hold"


@given(value=_RECURSIVE)
def test_canary_under_sensitive_key_never_appears(value: object) -> None:
    """Unrelated nesting cannot weaken redaction of a known secret."""
    sample = [value, {"nested": [{"ACCESS-TOKEN": CANARY}]}]
    assert CANARY not in DiagnosticSanitizer().format_sample(sample), (
        "the diagnostic safety contract must hold"
    )


class HostileMeta(type):
    """Metaclass display and equality hooks must not run either."""

    def __eq__(cls, other: object) -> bool:
        """Reject class comparisons."""
        msg = "metaclass equality invoked"
        raise AssertionError(msg)

    __hash__ = type.__hash__

    def __getattribute__(cls, name: str) -> object:
        """Reject reflected name access through the metaclass."""
        if name == "__qualname__":
            msg = "metaclass descriptor invoked"
            raise AssertionError(msg)
        return type.__getattribute__(cls, name)


class MetaValue(metaclass=HostileMeta):
    """An otherwise ordinary object with hostile type hooks."""


def test_metadata_bypasses_metaclass_hooks() -> None:
    """Safe type names use built-in descriptors and identity checks."""
    value = MetaValue()
    assert _type_name(value) == "MetaValue", "the diagnostic safety contract must hold"
    assert _frame_metadata(value) == ("object", None), (
        "the diagnostic safety contract must hold"
    )
    assert "MetaValue" in DiagnosticSanitizer().format_sample(value), (
        "the diagnostic safety contract must hold"
    )


def test_released_memoryview_fails_closed() -> None:
    """Even unusable binary buffers can be omitted safely."""
    value = memoryview(CANARY.encode())
    value.release()
    assert CANARY not in DiagnosticSanitizer().format_sample(value), (
        "the diagnostic safety contract must hold"
    )


class HostileSequence(list):  # ruff: ignore[subclass-builtin]  # deliberately test list-subclass rejection
    """Sequence subclasses must not supply their own traversal."""

    def __iter__(self) -> typ.Never:
        """Reject iteration."""
        msg = "iteration invoked"
        raise AssertionError(msg)

    def __len__(self) -> int:
        """Reject caller length access."""
        msg = "length invoked"
        raise AssertionError(msg)


class HostileString(str):  # ruff: ignore[subclass-builtin]  # deliberately test str-subclass rejection
    """String subclasses are untrusted even when their base value is text."""

    def casefold(self) -> str:
        """Reject normalization."""
        msg = "casefold invoked"
        raise AssertionError(msg)


def test_identifier_collection_omits_subclasses() -> None:
    """Only exact built-in collections may supply diagnostic field names."""
    assert _identifiers(HostileSequence([CANARY])) == "<omitted>", (
        "custom iterators must not execute"
    )
    assert _identifier(HostileString(CANARY)) == "<invalid>", (
        "custom strings must be omitted"
    )


@pytest.mark.parametrize(
    "value",
    [
        bytearray(CANARY.encode()),
        memoryview(CANARY.encode()),
        HostileString(CANARY),
        HostileSequence([CANARY]),
    ],
)
def test_binary_and_subclass_samples_omit_values(value: object) -> None:
    """Binary and subclass contents remain outside the sample boundary."""
    assert CANARY not in DiagnosticSanitizer().format_sample(value), (
        "unsafe values must be absent"
    )


def test_repeated_reference_is_not_a_cycle() -> None:
    """Cycle detection applies to the active path, not all previous objects."""
    shared = {"ok": "visible"}
    sample = DiagnosticSanitizer().sanitize([shared, shared])
    assert sample == [shared, shared], "both non-cyclic occurrences must be preserved"


def test_tuple_cycles_and_non_string_keys_are_counted() -> None:
    """Mixed containers and unknown keys are omitted without invoking methods."""
    # pylint: disable=prefer-snapshot-substring  # canary and traversal predicates are independent of sample formatting
    nested: list[object] = []
    sample = (nested,)
    nested.append(sample)
    rendered = DiagnosticSanitizer().format_sample({Hostile(): CANARY, "tuple": sample})
    assert CANARY not in rendered, "unknown-key values must be omitted"
    assert "<cycle>" in rendered, "cycles through tuple and list must terminate"
    assert "<omitted entries>" in rendered, "unknown entries must be counted"


@pytest.mark.parametrize(
    "keys", [frozenset({"___"}), frozenset({HostileString("token")})]
)
def test_uncertain_extension_keys_are_rejected(keys: frozenset[str]) -> None:
    """Custom normalization must not silently weaken the redaction set."""
    with pytest.raises(ValueError, match="extra_sensitive_keys"):
        DiagnosticSanitizer(extra_sensitive_keys=keys)
