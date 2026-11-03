"""Contracts for isolated release build inputs."""

import tomllib

import pytest
from packaging.requirements import Requirement

from tests._makefile import REPO_ROOT, makefile_recipe


def test_build_requirements_are_exact_pins() -> None:
    """Every executable build requirement has a reviewed version."""
    data = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())
    for item in data["build-system"]["requires"]:
        requirement = Requirement(item)
        specifiers = list(requirement.specifier)
        assert len(specifiers) == 1, f"Expected one exact pin: {item}"
        assert specifiers[0].operator == "==", f"Expected an exact pin: {item}"
        assert "*" not in specifiers[0].version, f"Wildcard pin: {item}"


@pytest.mark.parametrize("target", ["release-wheel", "build-release"])
def test_build_recipes_require_verified_isolated_inputs(target: str) -> None:
    """Both release routes require hashes in isolated, uncached builds."""
    recipe = makefile_recipe(target)
    for option in (
        "--build-constraint build-constraints.txt",
        "--require-build-hashes",
        "--no-cache",
    ):
        assert option in recipe, f"{target} lacks {option}"
    assert "--no-build-isolation" not in recipe, f"{target} must isolate builds"
