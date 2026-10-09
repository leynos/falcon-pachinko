"""Keep dependency bounds and published metadata consistent."""

import tomllib
from pathlib import Path

import pytest
from packaging.requirements import Requirement
from packaging.version import Version

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PYPROJECT = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
PROJECT = PYPROJECT["project"]
RUNTIME_REQUIREMENTS = [Requirement(item) for item in PROJECT["dependencies"]]
FALCON_REQUIREMENT = next(
    requirement for requirement in RUNTIME_REQUIREMENTS if requirement.name == "falcon"
)
DEVELOPMENT_CLASSIFIERS = {
    "a": "Development Status :: 3 - Alpha",
    "b": "Development Status :: 4 - Beta",
}


@pytest.mark.parametrize("requirement", RUNTIME_REQUIREMENTS)
def test_runtime_dependencies_have_bounded_versions(
    requirement: Requirement,
) -> None:
    """Runtime dependencies declare both a lower and exclusive upper bound."""
    operators = {specifier.operator for specifier in requirement.specifier}

    assert operators & {">", ">="}, f"{requirement.name} needs a lower bound"
    assert "<" in operators, f"{requirement.name} needs an exclusive upper bound"


@pytest.mark.parametrize(
    "version",
    [
        pytest.param("4.0.0", id="minimum-supported-release"),
        pytest.param("4.4.0", id="latest-ci-release"),
        pytest.param("4.99.0", id="latest-four-series-boundary"),
    ],
)
def test_falcon_requirement_accepts_supported_major(version: str) -> None:
    """Falcon's dependency range accepts supported 4.x versions."""
    assert Version(version) in FALCON_REQUIREMENT.specifier, (
        f"Falcon {version} must satisfy {FALCON_REQUIREMENT.specifier}"
    )


@pytest.mark.parametrize(
    "version",
    [
        pytest.param("3.99.0", id="previous-major"),
        pytest.param("5.0.0", id="next-major"),
    ],
)
def test_falcon_requirement_rejects_unsupported_majors(version: str) -> None:
    """Falcon's dependency range excludes the previous and next major."""
    assert Version(version) not in FALCON_REQUIREMENT.specifier, (
        f"Falcon {version} must not satisfy {FALCON_REQUIREMENT.specifier}"
    )


@pytest.mark.parametrize(
    ("version", "classifier"),
    [
        pytest.param("0.1.0-alpha2", DEVELOPMENT_CLASSIFIERS["a"], id="alpha"),
        pytest.param("0.2.0-beta1", DEVELOPMENT_CLASSIFIERS["b"], id="beta"),
    ],
)
def test_prerelease_version_maps_to_development_classifier(
    version: str, classifier: str
) -> None:
    """Alpha and beta versions map to their matching maturity classifiers."""
    parsed_version = Version(version)

    assert parsed_version.pre is not None, f"{version} must be a prerelease"
    actual_classifier = DEVELOPMENT_CLASSIFIERS[parsed_version.pre[0]]
    assert actual_classifier == classifier, (
        f"{version} must map to {classifier}, got {actual_classifier}"
    )


@pytest.mark.parametrize("field", ["authors", "keywords", "classifiers", "license"])
def test_required_project_metadata_is_present(field: str) -> None:
    """The published project metadata fields are present and non-empty."""
    assert PROJECT.get(field), f"pyproject.toml must define non-empty {field}"


def test_project_version_matches_development_classifier() -> None:
    """The current alpha version carries the Alpha development classifier."""
    version = Version(PROJECT["version"])

    assert version.pre is not None, "The project version must be a prerelease"
    expected_classifier = DEVELOPMENT_CLASSIFIERS[version.pre[0]]
    assert expected_classifier in PROJECT["classifiers"], (
        f"pyproject.toml must include {expected_classifier} for {version}"
    )


def test_project_has_async_keywords_and_discovery_classifiers() -> None:
    """PyPI metadata describes the project's asynchronous ASGI focus."""
    assert {
        "falcon",
        "websocket",
        "msgspec",
        "asyncio",
        "asgi",
        "websockets",
    } <= set(PROJECT["keywords"]), (
        "pyproject.toml keywords must include the supported project topics"
    )
    assert len(PROJECT["keywords"]) == len(set(PROJECT["keywords"])), (
        "pyproject.toml keywords must not contain duplicates"
    )
    assert {
        "Framework :: AsyncIO",
        "Operating System :: OS Independent",
        "Programming Language :: Python :: 3 :: Only",
        "Topic :: Software Development :: Libraries :: Python Modules",
    } <= set(PROJECT["classifiers"]), (
        "pyproject.toml classifiers must describe async and OS-independent use"
    )


def test_project_urls_identify_repository_and_issue_tracker() -> None:
    """The project page links to its source and issue tracker."""
    assert PROJECT["urls"]["Repository"] == (
        "https://github.com/leynos/falcon-pachinko"
    ), "The Repository URL must point to the GitHub project"
    assert PROJECT["urls"]["Issues"] == (
        "https://github.com/leynos/falcon-pachinko/issues"
    ), "The Issues URL must point to the GitHub issue tracker"
    assert PROJECT["urls"]["Documentation"] == (
        "https://github.com/leynos/falcon-pachinko/tree/main/docs"
    ), "The Documentation URL must point to the project documentation"


def test_packaging_is_a_development_dependency() -> None:
    """Metadata regression tests can rely on packaging in the dev group."""
    dev_requirements = [
        Requirement(item) for item in PYPROJECT["dependency-groups"]["dev"]
    ]

    assert any(requirement.name == "packaging" for requirement in dev_requirements), (
        "packaging must be declared in the dev dependency group"
    )
