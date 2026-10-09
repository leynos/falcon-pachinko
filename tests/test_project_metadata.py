"""Packaging metadata must match the supported project contract."""

from __future__ import annotations

import tomllib

import pytest
from packaging.requirements import Requirement
from packaging.specifiers import SpecifierSet
from packaging.version import Version

from tests._makefile import REPO_ROOT

PROJECT = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))[
    "project"
]
FALCON_REQUIREMENT = next(
    Requirement(dependency)
    for dependency in PROJECT["dependencies"]
    if Requirement(dependency).name == "falcon"
)


def test_falcon_dependency_has_lower_and_upper_bounds() -> None:
    """The Falcon 4 API dependency stays within a bounded version range."""
    specifier = FALCON_REQUIREMENT.specifier
    operators = {bound.operator for bound in specifier}

    assert operators & {">", ">="}, "Falcon must have a lower version bound"
    assert operators & {"<", "<="}, "Falcon must have an upper version bound"


@pytest.mark.parametrize(
    "version",
    [
        pytest.param("4.0.0", id="minimum-supported-release"),
        pytest.param("4.4.0", id="latest-ci-release"),
        pytest.param("4.99.0", id="latest-four-series-boundary"),
    ],
)
def test_falcon_dependency_accepts_four_series(version: str) -> None:
    """Representative Falcon 4 releases satisfy the declared dependency range."""
    assert FALCON_REQUIREMENT.specifier.contains(Version(version)), (
        f"Falcon {version} must satisfy {FALCON_REQUIREMENT.specifier}"
    )


@pytest.mark.parametrize(
    "version",
    [
        pytest.param("3.99.0", id="previous-major"),
        pytest.param("5.0.0", id="next-major"),
        pytest.param("5.1.0", id="representative-next-major-release"),
        pytest.param("5.99.0", id="latest-next-major-boundary"),
    ],
)
def test_falcon_dependency_rejects_other_major_series(version: str) -> None:
    """Falcon releases outside 4.x do not satisfy the dependency range."""
    assert not FALCON_REQUIREMENT.specifier.contains(Version(version)), (
        f"Falcon {version} must not satisfy {FALCON_REQUIREMENT.specifier}"
    )


def test_discoverability_metadata_is_present() -> None:
    """Published distributions retain their authorship and discovery fields."""
    for field in ("authors", "keywords", "classifiers", "urls"):
        assert PROJECT[field], f"project metadata field {field!r} must not be empty"
    assert PROJECT["description"] == (
        "Adds WebSocket routing and message handling for Falcon ASGI applications."
    ), "The description must accurately describe the package"
    assert "async" not in PROJECT["keywords"], (
        "The keywords must use 'asyncio' instead of 'async'"
    )
    assert "Programming Language :: Python :: 3.14" not in PROJECT["classifiers"], (
        "CI does not currently test Python 3.14"
    )


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
