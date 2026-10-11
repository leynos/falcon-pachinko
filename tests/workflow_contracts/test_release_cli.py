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
        ("stage-pypi", "provenance"),
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
        "stage-pypi": ("index", "assets", "github-files", "provenance"),
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


def test_missing_github_output_is_reported_by_the_cli_adapter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Missing workflow output configuration remains a contextual CLI error."""
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    publish = tmp_path / "publish"
    publish.mkdir()
    (publish / "package.whl").write_bytes(b"wheel")
    index = tmp_path / "index.json"
    index.write_text('{"files": []}', encoding="utf-8")
    assets = tmp_path / "assets.json"
    assets.write_text('{"assets": []}', encoding="utf-8")
    result = release_assets_cli.main([
        "stage-pypi",
        "--publish",
        str(publish),
        "--index",
        str(index),
        "--assets",
        str(assets),
        "--github-files",
        str(tmp_path),
        "--provenance",
        str(tmp_path),
    ])
    assert result == 1, "missing workflow output configuration must fail clearly"
    assert "GITHUB_OUTPUT" in capsys.readouterr().err, "identify the absent setting"


def test_output_writer_uses_the_injected_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Core output writing ignores ambient workflow environment settings."""
    destination = tmp_path / "injected-output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(tmp_path / "ambient-output"))
    release_assets_cli.write_output(destination, "remaining", value=True)
    release_assets_cli.write_output(destination, "pending", value=False)
    assert (
        destination.read_text(encoding="utf-8") == "remaining=true\npending=false\n"
    ), "append both outputs to the injected destination"
    assert not (tmp_path / "ambient-output").exists(), "ignore ambient configuration"
