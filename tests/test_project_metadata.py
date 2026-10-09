"""Packaging metadata must match the supported project contract."""

from __future__ import annotations

import tomllib

from packaging.requirements import Requirement
from packaging.specifiers import SpecifierSet
from packaging.version import Version

from tests._makefile import REPO_ROOT

PROJECT = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))[
    "project"
]


def test_falcon_dependency_has_bounds_that_exclude_version_five() -> None:
    """The package uses Falcon 4 APIs and must not admit a later major release."""
    requirement = next(
        Requirement(dependency)
        for dependency in PROJECT["dependencies"]
        if Requirement(dependency).name == "falcon"
    )
    specifier = requirement.specifier

    assert any(bound.operator in {">", ">="} for bound in specifier), (
        "Falcon must have a lower version bound"
    )
    assert any(bound.operator in {"<", "<="} for bound in specifier), (
        "Falcon must have an upper version bound"
    )
    assert not specifier.contains(Version("5.0.0")), (
        "The Falcon dependency must exclude version 5.0.0"
    )
    assert not specifier.contains(Version("5.99.0")), (
        "The Falcon dependency must exclude the 5.x series"
    )


def test_discoverability_metadata_is_present() -> None:
    """Published distributions retain their authorship and discovery fields."""
    for field in ("authors", "keywords", "classifiers", "urls"):
        assert PROJECT[field], f"project metadata field {field!r} must not be empty"


def test_python_version_classifiers_agree_with_requires_python() -> None:
    """Each minor-version classifier must be allowed by requires-python."""
    supported_versions = SpecifierSet(PROJECT["requires-python"])

    for classifier in PROJECT["classifiers"]:
        if classifier.startswith("Programming Language :: Python :: 3."):
            version = Version(classifier.rsplit(" :: ", maxsplit=1)[-1])
            assert version in supported_versions, (
                f"{classifier!r} is outside requires-python "
                f"{PROJECT['requires-python']!r}"
            )
