"""Safe structural metadata and explicitly opted-in diagnostic samples.

Omission is the security boundary. Redaction is only a defence-in-depth aid for
trusted local diagnostics: unknown field names and positional secrets remain
an application's responsibility.
"""

from __future__ import annotations

import dataclasses as dc
import itertools
import json
import math
import typing as typ

if typ.TYPE_CHECKING:
    import collections.abc as cabc

__all__ = ("DEFAULT_SENSITIVE_KEYS", "DiagnosticSanitizer")

_MAX_IDENTIFIER_LENGTH = 96
_MAX_IDENTIFIERS = 16
_MAX_KEY_LENGTH = 4096
_MAX_INTEGER = 2**63 - 1
_EVENT_NAMES = frozenset({
    "before_connect",
    "after_connect",
    "before_receive",
    "after_receive",
    "before_disconnect",
})
DEFAULT_SENSITIVE_KEYS = frozenset({
    "authorization",
    "password",
    "passwd",
    "passphrase",
    "token",
    "access_token",
    "refresh_token",
    "api_key",
    "secret",
    "credential",
    "cookie",
    "bearer",
    "private_key",
})


def _identifier(value: object) -> str:
    """Accept only short, exact printable ASCII identifiers."""
    if type(value) is not str:
        return "<invalid>"
    if len(value) > _MAX_IDENTIFIER_LENGTH:
        return "<long>"
    if not value or not all(" " <= char <= "~" for char in value):
        return "<invalid>"
    return value


def _identifiers(values: cabc.Collection[object]) -> str:
    """Bound names before sorting and rendering framework field metadata."""
    if not any(type(values) is kind for kind in (dict, list, tuple, set, frozenset)):
        return "<omitted>"
    names = sorted(
        _identifier(value) for value in itertools.islice(values, _MAX_IDENTIFIERS)
    )
    rendered = "[" + ", ".join(json.dumps(name) for name in names) + "]"
    overflow = len(values) - len(names)
    if overflow:
        rendered += f" (+{overflow} fields)"
    return rendered


def _class_name(cls: type) -> str:
    """Read the built-in type descriptor without a caller's metaclass hook."""
    return _identifier(type.__getattribute__(cls, "__qualname__"))


def _type_name(value: object) -> str:
    """Describe an object's type without invoking object display methods."""
    return _class_name(type(value))


def _frame_metadata(value: object) -> tuple[str, int | None]:
    """Describe an exact built-in frame without decoding or inspecting it."""
    if type(value) is str:
        return "text", len(value)
    if type(value) is bytes or type(value) is bytearray:
        return "bytes", len(value)
    if type(value) is memoryview:
        try:
            length = value.nbytes
        except ValueError:  # Released buffers have no safely accessible length.
            length = None
        return "bytes", length
    return ("none", None) if value is None else ("object", None)


def _safe_scalar(value: object) -> str:
    """Render only trusted primitive scalars and known lifecycle events."""
    # Delayed import avoids a diagnostics/hooks import cycle.
    from .hooks import HookEvent

    if value is None:
        return "None"
    if type(value) is bool:
        return "True" if value else "False"
    if type(value) is int:
        return f"{value}" if -_MAX_INTEGER <= value <= _MAX_INTEGER else "<integer>"
    if type(value) is HookEvent:
        return value.value
    if type(value) is str and value in _EVENT_NAMES:
        return value
    return "<omitted>"


def _normalize_key(key: str) -> str:
    """Normalize common separators and casing for fragment matching."""
    return "".join(char for char in key.casefold() if char.isalnum())


@dc.dataclass(frozen=True, slots=True)
class DiagnosticSanitizer:
    """Configure an explicitly opted-in, bounded diagnostic sample.

    Only exact built-in containers and scalars are traversed. Binary values are
    described by length; unsupported objects and uncertain keys are omitted.
    Limits also have hard ceilings to keep traversal within Python's stack and
    avoid accidentally configuring effectively unbounded diagnostics.

    Parameters
    ----------
    max_depth : int
        Maximum container depth, from zero to 32. Defaults to 6.
    max_items : int
        Maximum entries inspected per collection, from zero to 256. Defaults
        to 20; an omission count can add one synthetic entry.
    max_string_length : int
        Maximum sample string or key length, from zero to 4096. Defaults to
        128. Values under oversized keys are omitted.
    max_output_length : int
        Total formatted output budget, from zero to 65536. Defaults to 2048.
        Traversal stops when this budget is exhausted.
    extra_sensitive_keys : frozenset[str]
        Additional nonempty, bounded sensitive fragments, normalized once.
        Defaults to an empty set; the default fragments always remain active.

    Raises
    ------
    ValueError
        If a bound is invalid or a fragment is empty, oversized, or uncertain.
    TypeError
        If ``extra_sensitive_keys`` is not an exact ``frozenset``.
    """

    max_depth: int = 6
    max_items: int = 20
    max_string_length: int = 128
    max_output_length: int = 2048
    extra_sensitive_keys: frozenset[str] = frozenset()
    _sensitive_fragments: frozenset[str] = dc.field(init=False, repr=False)

    def __post_init__(self) -> None:
        """Validate bounds and normalize custom fragments once."""
        bounds = (
            (self.max_depth, 32),
            (self.max_items, 256),
            (self.max_string_length, 4096),
            (self.max_output_length, 65536),
        )
        if any(
            type(value) is not int or not 0 <= value <= ceiling
            for value, ceiling in bounds
        ):
            msg = "Diagnostic sanitizer bound outside supported range"
            raise ValueError(msg)
        if type(self.extra_sensitive_keys) is not frozenset:
            msg = "extra_sensitive_keys must be an exact frozenset of strings"
            raise TypeError(msg)
        if any(
            type(key) is not str or not key or len(key) > _MAX_KEY_LENGTH
            for key in self.extra_sensitive_keys
        ):
            msg = "extra_sensitive_keys must contain bounded nonempty strings"
            raise ValueError(msg)
        fragments = frozenset(
            _normalize_key(key)
            for key in DEFAULT_SENSITIVE_KEYS | self.extra_sensitive_keys
        )
        if "" in fragments:
            msg = "extra_sensitive_keys must contain nonempty normalized keys"
            raise ValueError(msg)
        object.__setattr__(self, "_sensitive_fragments", fragments)

    def sanitize(self, value: object) -> object:
        """Return a sanitized built-in tree within the configured bounds.

        Parameters
        ----------
        value : object
            The trusted application's explicitly selected diagnostic sample.

        Returns
        -------
        object
            A bounded tree of exact built-ins and omission placeholders.
            With a zero output budget, returns ``"<budget>"`` immediately.
            This method does not mutate the input.
        """
        return _SampleBuilder(self).walk(value, 0)

    def format_sample(self, value: object) -> str:
        """Format a sanitized sample incrementally within the output budget.

        Parameters
        ----------
        value : object
            The trusted application's explicitly selected diagnostic sample.

        Returns
        -------
        str
            JSON-like sample text no longer than ``max_output_length``.
            Budget truncation can leave incomplete JSON; a zero budget returns
            an empty string. Binary values are never decoded.
        """
        builder = _SampleBuilder(self)
        builder.walk(value, 0)
        return "".join(builder.chunks)


@dc.dataclass(slots=True)
class _SampleBuilder:
    """Share traversal and output budgets without reflecting caller values."""

    config: DiagnosticSanitizer
    chunks: list[str] = dc.field(default_factory=list)
    active: set[int] = dc.field(default_factory=set)
    remaining: int = dc.field(init=False)

    def __post_init__(self) -> None:
        self.remaining = self.config.max_output_length

    def _emit(self, text: str) -> None:
        """Append only as much trusted text as the remaining budget permits."""
        chunk = text[: self.remaining]
        self.chunks.append(chunk)
        self.remaining -= len(chunk)

    def _scalar(self, value: object) -> object:
        """Select supported scalars without calling custom conversion methods."""
        if value is None or type(value) is bool:
            return value
        if type(value) is str:
            return value[: self.config.max_string_length]
        if type(value) is int:
            return value if -_MAX_INTEGER <= value <= _MAX_INTEGER else "<integer>"
        if type(value) is float:
            return value if math.isfinite(value) else "<float>"
        kind, length = _frame_metadata(value)
        return (
            f"<bytes len={length}>"
            if kind == "bytes"
            else f"<omitted {_type_name(value)}>"
        )

    def _leaf(self, value: object) -> object:
        """Render safe primitives only, using JSON escaping for sample text."""
        self._emit(json.dumps(value, ensure_ascii=True))
        return value

    def walk(self, value: object, depth: int) -> object:
        """Bound traversal before accessing exact built-in containers."""
        if not self.remaining:
            return "<budget>"
        if not any(type(value) is kind for kind in (dict, list, tuple)):
            return self._leaf(self._scalar(value))
        if id(value) in self.active:
            return self._leaf("<cycle>")
        if depth >= self.config.max_depth:
            return self._leaf("<depth>")
        self.active.add(id(value))
        try:
            return self._container(value, depth)
        finally:
            self.active.remove(id(value))

    def _container(self, value: object, depth: int) -> object:
        """Preserve container shape while omitting overflow and uncertain keys."""
        if type(value) is dict:
            # The exact-type guard permits all key/value types; dict is invariant.
            return self._mapping(typ.cast("dict[object, object]", value), depth)
        # Exact-type checks in walk guarantee built-in iteration only.
        sequence = typ.cast("list[object] | tuple[object, ...]", value)
        self._emit("[")
        result: list[object] = []
        for item in itertools.islice(sequence, self.config.max_items):
            if not self.remaining:
                break
            if result:
                self._emit(", ")
            result.append(self.walk(item, depth + 1))
        if len(sequence) > len(result):
            self._emit(", " if result else "")
            result.append(self._leaf(f"<omitted {len(sequence) - len(result)} items>"))
        self._emit("]")
        return tuple(result) if type(value) is tuple else result

    def _mapping(self, value: dict[object, object], depth: int) -> dict[str, object]:
        """Omit non-string keys, and redact normalized sensitive fragments."""
        self._emit("{")
        result: dict[str, object] = {}
        emitted = 0
        for key, item in itertools.islice(value.items(), self.config.max_items):
            if not self.remaining:
                break
            if type(key) is not str:
                continue
            name = key[: self.config.max_string_length]
            if name in result:
                continue
            if result:
                self._emit(", ")
            self._emit(json.dumps(name) + ": ")
            result[name] = self._mapping_value(key, item, depth)
            emitted += 1
        omitted = len(value) - emitted
        if omitted:
            self._emit(", " if result else "")
            count_name = _omission_name(result)
            self._emit(json.dumps(count_name) + f": {omitted}")
            result[count_name] = omitted
        self._emit("}")
        return result

    def _mapping_value(self, key: str, value: object, depth: int) -> object:
        """Never display a value when a key cannot be matched with certainty."""
        if len(key) > self.config.max_string_length:
            return self._leaf("<omitted>")
        normalized = _normalize_key(key)
        if any(fragment in normalized for fragment in self.config._sensitive_fragments):
            return self._leaf("<redacted>")
        return self.walk(value, depth + 1)


def _omission_name(result: dict[str, object]) -> str:
    """Choose a synthetic count key without overwriting sample fields."""
    name = "<omitted entries>"
    suffix = 2
    while name in result:
        name = f"<omitted entries {suffix}>"
        suffix += 1
    return name
