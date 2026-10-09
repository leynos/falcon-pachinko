"""Shared schema helpers and findings for the nose duplication gate.

This module owns the gate's error vocabulary, the gate-neutral finding
types, and the validation of the ``nose query --format json`` report.
``scripts/nose_detector.py`` drives the binary itself, and
``scripts/duplication_gate.py`` owns the reasoned allowlist and CLI.

Adapted from ``leynos/episodic`` PR #276 at commit
``d9e5ac0d254f375e2986f52d91a3b88c117c833b``.
"""

from __future__ import annotations

import dataclasses as dc
import math
import typing as typ
from collections import abc as cabc
from pathlib import PurePosixPath

NOSE_SCHEMA_VERSION = 9


class GateConfigError(ValueError):
    """Raised when the gate configuration or the detector report is malformed."""


class GateExecutionError(GateConfigError):
    """Raised when the pinned detector is missing, wrong, or fails to run."""


@dc.dataclass(frozen=True, slots=True)
class Location:
    """One duplicated region reported by nose.

    Attributes
    ----------
    file : str
        Repository-relative POSIX path of the duplicated region.
    start, end : int
        Inclusive first and last source line of the region.
    name : str | None
        Unit name when nose matched a whole function, class, or method;
        ``None`` for fragment-level matches such as import blocks.
    """

    file: str
    start: int
    end: int
    name: str | None

    @property
    def span(self) -> str:
        """Inclusive ``path:start-end`` source span."""
        return f"{self.file}:{self.start}-{self.end}"

    @property
    def label(self) -> str:
        """Source span suffixed with nose's unit name, when present."""
        return self.span if self.name is None else f"{self.span} {self.name}"


@dc.dataclass(frozen=True, slots=True)
class Finding:
    """One duplication family in gate-neutral form.

    Attributes
    ----------
    witness : str
        nose evidence kind (``exact``, ``copy-paste``, ``similar``, ...).
    value : float
        nose refactoring value; the gate reports it and orders by it.
    locations : tuple[Location, ...]
        Every duplicated region in the family, in report order.
    """

    witness: str
    value: float
    locations: tuple[Location, ...]

    @property
    def label(self) -> str:
        """The ``path:lines ~ path:lines`` summary of the family."""
        return " ~ ".join(location.label for location in self.locations)


def require_table(value: object, *, context: str) -> cabc.Mapping[str, object]:
    """Validate one TOML table before configuration logic consumes it.

    Parameters
    ----------
    value : object
        Candidate TOML value.
    context : str
        Configuration path used in an invalid-value diagnostic.

    Returns
    -------
    collections.abc.Mapping[str, object]
        The validated table with string keys.

    Raises
    ------
    GateConfigError
        If ``value`` is not a table with string keys.
    """
    if not isinstance(value, cabc.Mapping) or not all(
        isinstance(key, str) for key in value
    ):
        msg = f"{context} must be a table with string keys"
        raise GateConfigError(msg)
    return typ.cast("cabc.Mapping[str, object]", value)


def is_safe_relative_posix_path(path_value: str) -> bool:
    """Report whether a path uses canonical, non-escaping POSIX syntax."""
    path = PurePosixPath(path_value)
    if path.is_absolute() or "\\" in path_value:
        return False
    if ".." in path.parts:
        return False
    return path.as_posix() == path_value


def require_value[T](
    value: object,
    *,
    validator: cabc.Callable[[object], typ.TypeIs[T]],
    expected: str,
    context: str,
) -> T:
    """Validate a value with one predicate and a contextual diagnostic.

    Parameters
    ----------
    value : object
        Candidate value from configuration or a detector report.
    validator : collections.abc.Callable[[object], typing.TypeIs[T]]
        Type guard defining the accepted value.
    expected : str
        Human-readable value description used in the diagnostic.
    context : str
        Configuration or report path used in an invalid-value diagnostic.

    Returns
    -------
    T
        The validated value.

    Raises
    ------
    GateConfigError
        If ``value`` does not satisfy ``validator``.
    """
    if not validator(value):
        msg = f"{context} must be {expected}"
        raise GateConfigError(msg)
    return typ.cast("T", value)


def require_string_tuple(value: object, *, context: str) -> tuple[str, ...]:
    """Validate one configuration array of non-empty strings.

    Parameters
    ----------
    value : object
        Candidate configuration value.
    context : str
        Configuration path used in an invalid-value diagnostic.

    Returns
    -------
    tuple[str, ...]
        The validated configuration strings in input order.

    Raises
    ------
    GateConfigError
        If ``value`` is not an array of non-empty strings.
    """
    if not isinstance(value, cabc.Sequence) or isinstance(value, (str, bytes)):
        msg = f"{context} must be an array of strings"
        raise GateConfigError(msg)
    return tuple(
        require_value(
            item,
            validator=is_non_empty_string,
            expected="a non-empty string",
            context=f"{context}[]",
        )
        for item in value
    )


def is_non_empty_string(value: object) -> typ.TypeIs[str]:
    """Report whether ``value`` is a non-empty string."""
    return isinstance(value, str) and bool(value)


def _is_integer(value: object) -> typ.TypeIs[int]:
    """Report whether ``value`` is an integer rather than a boolean."""
    return isinstance(value, int) and not isinstance(value, bool)


def is_positive_integer(value: object) -> typ.TypeIs[int]:
    """Report whether ``value`` is a positive integer rather than a boolean."""
    return _is_integer(value) and value > 0


def normalize_findings(report: object) -> list[Finding]:
    """Convert one validated nose report into ordered gate findings.

    Parameters
    ----------
    report : object
        Decoded ``nose query --format json`` report.

    Returns
    -------
    list[Finding]
        Findings ordered by descending value, then by source location.

    Raises
    ------
    GateConfigError
        If the report does not match the expected schema.
    """
    report_table = require_table(report, context="nose report")
    schema_version = report_table.get("schema_version")
    if not _is_integer(schema_version) or schema_version != NOSE_SCHEMA_VERSION:
        msg = "nose report schema_version must be 9"
        raise GateConfigError(msg)
    families = report_table.get("families")
    if not isinstance(families, cabc.Sequence) or isinstance(families, (str, bytes)):
        msg = "nose report families must be an array"
        raise GateConfigError(msg)
    findings = [
        _finding(family, context=f"nose report families[{index}]")
        for index, family in enumerate(families)
    ]
    findings.sort(key=lambda finding: (-finding.value, finding.label, finding.witness))
    return findings


def _finding_value(value: object, *, context: str) -> float:
    """Validate a family's refactoring value as a non-boolean number."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        msg = f"{context}.value must be a number"
        raise GateConfigError(msg)
    normalized = float(value)
    if not math.isfinite(normalized):
        msg = f"{context}.value must be finite"
        raise GateConfigError(msg)
    return normalized


def _finding_locations(value: object, *, context: str) -> tuple[Location, ...]:
    """Validate a family's member locations, preserving their report order."""
    if not isinstance(value, cabc.Sequence) or isinstance(value, (str, bytes)):
        msg = f"{context}.locations must be an array"
        raise GateConfigError(msg)
    if not value:
        msg = f"{context}.locations must not be empty"
        raise GateConfigError(msg)
    return tuple(
        _location(location, context=f"{context}.locations[{index}]")
        for index, location in enumerate(value)
    )


def _finding(raw: object, *, context: str) -> Finding:
    """Validate one nose family payload."""
    family = require_table(raw, context=context)
    return Finding(
        witness=require_value(
            family.get("witness"),
            validator=is_non_empty_string,
            expected="a non-empty string",
            context=f"{context}.witness",
        ),
        value=_finding_value(family.get("value"), context=context),
        locations=_finding_locations(family.get("locations"), context=context),
    )


def _location(raw: object, *, context: str) -> Location:
    """Validate one nose location payload."""
    location = require_table(raw, context=context)
    start = require_value(
        location.get("start"),
        validator=is_positive_integer,
        expected="a positive integer",
        context=f"{context}.start",
    )
    end = _location_end(location.get("end"), start, context=context)
    name = _location_name(location.get("name"), context=context)
    file = _location_file(location.get("file"), context=context)
    return Location(file=file, start=start, end=end, name=name)


def _location_end(value: object, start: int, *, context: str) -> int:
    """Validate the inclusive end line against a location's start line."""
    if not _is_integer(value) or value < start:
        msg = f"{context}.end must not precede start"
        raise GateConfigError(msg)
    return value


def _location_name(value: object, *, context: str) -> str | None:
    """Validate nose's optional unit name, preserving unnamed fragments."""
    if value is None:
        return None
    if not is_non_empty_string(value):
        msg = f"{context}.name must be a non-empty string or null"
        raise GateConfigError(msg)
    return value


def _location_file(value: object, *, context: str) -> str:
    """Validate a location's repository-relative Python source path."""
    raw_file = require_value(
        value,
        validator=is_non_empty_string,
        expected="a non-empty string",
        context=f"{context}.file",
    )
    path = PurePosixPath(raw_file)
    if not _is_repository_relative_python_path(path, raw_file):
        msg = f"{context}.file must be a repository-relative POSIX path"
        raise GateConfigError(msg)
    return path.as_posix()


def _is_repository_relative_python_path(path: PurePosixPath, raw: str) -> bool:
    """Report whether a nose file path is canonical, local, and Python source."""
    if path.is_absolute() or "\\" in raw:
        return False
    if ".." in path.parts or "." in path.parts:
        return False
    if path.as_posix() != raw:
        return False
    return path.suffix == ".py"
