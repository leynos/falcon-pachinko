"""Verify diagnostic omission, recursive redaction, and resource bounds.

Hostile objects detect accidental calls to application display and traversal
hooks. Canary secrets establish omission independently of sample formatting.
Run this module with ``uv run pytest tests/test_diagnostics_unit.py``.
"""

from __future__ import annotations

import dataclasses as dc
import json
import typing as typ

import pytest
from hypothesis import given
from hypothesis import strategies as st

from falcon_pachinko import DEFAULT_SENSITIVE_KEYS, DiagnosticSanitizer, diagnostics
from falcon_pachinko.diagnostics import (
    _class_name,
    _frame_metadata,
    _identifier,
    _identifiers,
    _safe_scalar,
    _type_name,
)
from falcon_pachinko.hooks import HookEvent
from tests._stubs import Hostile

if typ.TYPE_CHECKING:
    import collections.abc as cabc

CANARY = "CANARY-local-auth-74b1"


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
        "frame metadata must match the exact built-in kind and length"
    )


@pytest.mark.parametrize(
    ("renderer", "value", "expected", "message"),
    [
        (_type_name, Hostile(), "Hostile", "report only the hostile object's type"),
        (_identifier, Hostile(), "<invalid>", "reject non-string identifiers"),
        (_identifier, "x" * 1000, "<long>", "omit oversized identifiers"),
        (_identifier, "bad\nname", "<invalid>", "reject non-printable identifiers"),
        (_safe_scalar, Hostile(), "<omitted>", "omit unsupported scalar objects"),
        (_safe_scalar, 10**1000, "<integer>", "bound integer rendering"),
        (_safe_scalar, HookEvent.AFTER_RECEIVE, "after_receive", "render known events"),
        (_safe_scalar, CANARY, "<omitted>", "omit arbitrary string values"),
        (_identifiers, {"z", "a"}, "['a', 'z']", "sort rendered field names"),
    ],
)
def test_metadata_omits_hostile_and_oversized_values(
    renderer: cabc.Callable[[typ.Any], str], value: object, expected: str, message: str
) -> None:
    """Metadata never converts caller objects or long integers."""
    assert renderer(value) == expected, message


def test_field_metadata_bounds_collection_output() -> None:
    """Large field collections disclose a bounded set of identifiers."""
    assert len(_identifiers({f"field{i}" for i in range(1000)})) < 1500, (
        "field metadata must bound collection output"
    )


@pytest.mark.parametrize(
    "key",
    [
        *sorted(DEFAULT_SENSITIVE_KEYS),
        "AUTHORIZATION",
        "x-access.Token",
        "Refresh_Token",
        "Api-Key",
        "api:key",
        "x/access+token",
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
    assert CANARY not in rendered, (
        "nested sensitive fragments must never disclose the canary"
    )
    assert "<redacted>" in rendered, "sensitive values must use the redaction marker"


def test_sanitizer_preserves_safe_sample_and_extends_keys() -> None:
    """Opt-in output can include ordinary values and custom redaction."""
    sanitizer = DiagnosticSanitizer(extra_sensitive_keys=frozenset({"Tenant-Key"}))
    assert sanitizer.sanitize({"ok": "visible", "tenant_key": CANARY}) == {
        "ok": "visible",
        "tenant_key": "<redacted>",
    }, "extended keys must redact while preserving ordinary sample fields"
    assert "visible" in sanitizer.format_sample({"ok": "visible"}), (
        "explicit samples may display ordinary string values"
    )
    field = "max_depth"
    with pytest.raises(dc.FrozenInstanceError):
        setattr(sanitizer, field, 1)


def test_sanitizer_omits_unsupported_keys_bytes_and_cycles() -> None:
    """Uncertain values and binary frames never invoke decoding or display."""
    sample: list[object] = [Hostile(), HostileDict(token=CANARY), CANARY.encode()]
    sample.append(sample)
    rendered = DiagnosticSanitizer().format_sample({"items": sample, 1: CANARY})
    assert CANARY not in rendered, (
        "unsupported keys and byte contents must not disclose the canary"
    )
    assert all(
        marker in rendered for marker in ("<cycle>", "<bytes len=", "Hostile")
    ), "samples must describe cycles, binary lengths, and unsupported types"


def test_sanitizer_bounds_each_dimension() -> None:
    """Every container, scalar, and output budget is independently bounded."""
    sanitizer = DiagnosticSanitizer(max_depth=1, max_items=2, max_string_length=4)
    assert sanitizer.sanitize([[[CANARY]]]) == ["<depth>"], (
        "containers at the configured depth must use the depth marker"
    )
    assert sanitizer.sanitize("abcdef") == "abcd", (
        "sample string values must respect the configured character bound"
    )
    assert sanitizer.sanitize(10**1000) == "<integer>", (
        "oversized integers must use the integer marker"
    )
    sample = sanitizer.sanitize(list(range(100)))
    assert isinstance(sample, list), "sanitization must preserve list container shape"
    assert len(sample) == 3, "collection overflow must append one omission count marker"
    assert len(sanitizer.format_sample({"x": "abc" * 10000})) < 100, (
        "large strings must be bounded before formatting"
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
    ), "recursive formatted samples must never exceed the total budget"


@given(value=_RECURSIVE, budget=st.integers(min_value=1, max_value=200))
def test_sanitized_tree_obeys_total_budget_or_uses_omission_marker(
    value: object, budget: int
) -> None:
    """The returned tree must not retain values beyond its output allowance."""
    sanitized = DiagnosticSanitizer(max_output_length=budget).sanitize(value)
    if sanitized == "<budget>":
        return
    assert len(json.dumps(sanitized, ensure_ascii=True)) <= budget, (
        "sanitized trees must fit the output budget or collapse to an omission marker"
    )


@pytest.mark.parametrize(
    ("value", "budget"),
    [(CANARY, 1), ({"x": CANARY}, 8)],
)
def test_sanitize_omits_values_that_do_not_fit_total_budget(
    value: object, budget: int
) -> None:
    """A short total allowance must not return an oversized diagnostic tree."""
    assert (
        DiagnosticSanitizer(max_output_length=budget).sanitize(value) == "<budget>"
    ), "values that exceed the complete tree budget must become an omission marker"


def test_sanitize_collapses_mapping_cut_off_by_output_budget() -> None:
    """A partially visited mapping must not look like a complete sample."""
    sample = {"first": "visible", "second": "not inspected"}
    sanitizer = DiagnosticSanitizer(max_output_length=24)

    assert sanitizer.sanitize(sample) == "<budget>", (
        "mid-mapping budget exhaustion must collapse the partial tree"
    )


@given(value=_RECURSIVE)
def test_canary_under_sensitive_key_never_appears(value: object) -> None:
    """Unrelated nesting cannot weaken redaction of a known secret."""
    sample = [value, {"nested": [{"ACCESS-TOKEN": CANARY}]}]
    assert CANARY not in DiagnosticSanitizer().format_sample(sample), (
        "sensitive fields in recursive samples must never disclose the canary"
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
    assert _type_name(value) == "MetaValue", (
        "type metadata must bypass hostile metaclass attribute hooks"
    )
    assert _frame_metadata(value) == ("object", None), (
        "unsupported frame objects must omit length without calling hooks"
    )
    assert "MetaValue" in DiagnosticSanitizer().format_sample(value), (
        "unsupported samples must display only the safe type name"
    )


def test_released_memoryview_fails_closed() -> None:
    """Even unusable binary buffers can be omitted safely."""
    value = memoryview(CANARY.encode())
    value.release()
    assert CANARY not in DiagnosticSanitizer().format_sample(value), (
        "released binary views must omit inaccessible contents"
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
    nested: list[object] = []
    sample = (nested,)
    nested.append(sample)
    rendered = DiagnosticSanitizer().format_sample({Hostile(): CANARY, "tuple": sample})
    assert CANARY not in rendered, "unknown-key values must be omitted"
    assert all(marker in rendered for marker in ("<cycle>", "<omitted entries>")), (
        "tuple/list cycles must terminate and unknown entries must be counted"
    )


@pytest.mark.parametrize(
    "keys", [frozenset({"___"}), frozenset({HostileString("token")})]
)
def test_uncertain_extension_keys_are_rejected(keys: frozenset[str]) -> None:
    """Custom normalization must not silently weaken the redaction set."""
    with pytest.raises(ValueError, match="extra_sensitive_keys"):
        DiagnosticSanitizer(extra_sensitive_keys=keys)


def test_colliding_sample_keys_are_omitted_and_counted() -> None:
    """A sample never emits ambiguous duplicate truncated field names."""
    sanitizer = DiagnosticSanitizer(max_string_length=4)
    pairs = json.loads(
        sanitizer.format_sample({"aaaa1": CANARY, "aaaa2": CANARY}),
        object_pairs_hook=list,
    )
    assert pairs == [("aaaa", "<omitted>"), ("<omitted entries>", 1)], (
        "colliding keys must be counted without duplicate output"
    )


def test_omission_count_does_not_overwrite_a_real_field() -> None:
    """Synthetic metadata must preserve a legitimate same-named field."""
    sample = DiagnosticSanitizer().sanitize({"<omitted entries>": "visible", 1: CANARY})
    assert sample == {"<omitted entries>": "visible", "<omitted entries 2>": 1}, (
        "synthetic counts must not collide with application keys"
    )


@given(separator=st.text(alphabet="/:+!|.-_ ", min_size=1, max_size=3))
def test_key_separators_cannot_bypass_redaction(separator: str) -> None:
    """Common punctuation between key letters cannot expose API keys."""
    key = separator.join("APIKEY")
    assert CANARY not in DiagnosticSanitizer().format_sample({key: CANARY}), (
        "separator variants must remain redacted"
    )


@pytest.mark.parametrize(
    ("schema", "expected"),
    [(int, "int"), (int | str, type(int | str).__qualname__), (Hostile(), "Hostile")],
)
def test_expected_schema_metadata_handles_non_class_objects(
    schema: object, expected: str
) -> None:
    """Union schemas and unsupported objects disclose only safe type metadata."""
    assert _class_name(schema) == expected, (
        "schema metadata must fail closed without assuming a class object"
    )


@pytest.mark.parametrize(
    "value",
    [{"token": CANARY}, [CANARY], (CANARY,)],
)
def test_exhausted_budget_omits_over_budget_containers(value: object) -> None:
    """Even an empty sanitized container must fit the configured tree budget."""
    sanitizer = DiagnosticSanitizer(max_output_length=1)
    assert sanitizer.sanitize(value) == "<budget>", (
        "over-budget container trees must collapse to the omission marker"
    )


@pytest.mark.parametrize("value", [True, 1.5, -1, 33, Hostile()])
def test_bound_validator_rejects_unsupported_values(value: object) -> None:
    """An extracted bound validator preserves exact-integer checks."""
    with pytest.raises(ValueError, match="bound outside supported range"):
        diagnostics._validate_bound(value, 32)


@pytest.mark.parametrize("value", [0, 32])
def test_bound_validator_accepts_endpoints(value: int) -> None:
    """Both inclusive endpoints are valid configuration bounds."""
    assert diagnostics._validate_bound(value, 32) is None, (
        "the inclusive integer bound endpoints must remain valid"
    )


@pytest.mark.parametrize("value", ["", "___", "x" * 4097, Hostile()])
def test_fragment_validator_rejects_uncertain_keys(value: object) -> None:
    """New normalization helpers must reject uncertain key fragments."""
    with pytest.raises(ValueError, match="extra_sensitive_keys"):
        diagnostics._normalized_fragment(value)


def test_fragment_validator_normalizes_valid_key() -> None:
    """Normalization preserves case and separator matching behaviour."""
    assert diagnostics._normalized_fragment("API-Key") == "apikey", (
        "valid fragments must normalize before containment matching"
    )
