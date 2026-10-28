"""Specify release version matching and classification."""

from __future__ import annotations

import typing as typ

import pytest

from scripts import release_version

if typ.TYPE_CHECKING:
    import pathlib


@pytest.mark.parametrize(
    ("version", "expected"),
    [("0.1.0-alpha3", True), ("1.0.0", False), ("1.0.0+rc", False)],
)
def test_prerelease_classification(version: str, *, expected: bool) -> None:
    """Classify public prerelease markers without treating local labels as such."""
    assert release_version.is_prerelease(version) is expected, version


def test_tag_matches_exact_version(tmp_path: pathlib.Path) -> None:
    """The v-prefixed tag yields the exact declared string and its classification."""
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nversion = "0.1.0-alpha3"\n', encoding="utf-8"
    )
    output = tmp_path / "output"
    assert (
        release_version.main(
            {"GITHUB_REF_NAME": "v0.1.0-alpha3", "GITHUB_OUTPUT": str(output)}, tmp_path
        )
        == 0
    ), "Release contract must hold"
    assert output.read_text(encoding="utf-8").splitlines() == [
        "version=0.1.0-alpha3",
        "prerelease=true",
    ], "Release contract must hold"


def test_normalized_spelling_does_not_match(tmp_path: pathlib.Path) -> None:
    """PEP 440 equivalence does not relax the exact tag requirement."""
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nversion = "0.1.0-alpha3"\n', encoding="utf-8"
    )
    assert (
        release_version.main(
            {"GITHUB_REF_NAME": "v0.1.0a3", "GITHUB_OUTPUT": str(tmp_path / "output")},
            tmp_path,
        )
        == 1
    ), "Release contract must hold"
