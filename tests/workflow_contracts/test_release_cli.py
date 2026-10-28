"""Reject incomplete release reconciliation commands before touching files."""

from __future__ import annotations

import typing as typ

import pytest

from scripts import release_assets_cli

if typ.TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.parametrize(
    ("command", "missing"),
    [
        ("carryover", "assets"),
        ("stage-pypi", "index"),
        ("stage-pypi", "assets"),
        ("stage-pypi", "github-files"),
        ("stage-github", "index"),
        ("stage-github", "assets"),
        ("stage-github", "bundles"),
        ("stage-github", "output"),
        ("check-fetched", "index"),
        ("check-fetched", "assets"),
        ("check-fetched", "output"),
        ("index-expected", "index"),
        ("verify", "index"),
        ("verify", "assets"),
        ("verify", "bundles"),
    ],
)
def test_missing_input_fails_before_reading_state(
    tmp_path: Path, command: str, missing: str, capsys: pytest.CaptureFixture[str]
) -> None:
    """A missing command-specific input cannot be mistaken for an empty listing."""
    options = {
        "carryover": ("assets",),
        "stage-pypi": ("index", "assets", "github-files"),
        "stage-github": ("index", "assets", "bundles", "output"),
        "check-fetched": ("index", "assets", "output"),
        "index-expected": ("index",),
        "verify": ("index", "assets", "bundles"),
    }
    args = [command, "--publish", str(tmp_path / "absent")]
    for option in options[command]:
        if option != missing:
            args.extend([f"--{option}", str(tmp_path / "absent")])
    assert release_assets_cli.main(args) == 1, "missing input must fail"
    assert f"--{missing} is required" in capsys.readouterr().err, missing


def test_parser_works_without_module_docstring(monkeypatch: pytest.MonkeyPatch) -> None:
    """Optimized Python can strip module docstrings without breaking the CLI."""
    monkeypatch.setattr(release_assets_cli, "__doc__", None)
    with pytest.raises(SystemExit) as result:
        release_assets_cli.main(["--help"])
    assert result.value.code == 0, "the parser must work without a docstring"
