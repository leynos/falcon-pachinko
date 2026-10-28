"""Exercise the checked-in distribution cardinality check."""

from __future__ import annotations

import os
import pathlib
import shutil
import subprocess  # ruff: ignore[suspicious-subprocess-import] - execute the checked-in Bash step
import sys

import pytest
import yaml

from .strict_yaml import StrictLoader

ROOT = pathlib.Path(__file__).resolve().parents[2]
BASH = shutil.which("bash")
pytestmark = pytest.mark.skipif(
    sys.platform == "win32" or BASH is None, reason="The CI step requires Bash."
)


@pytest.mark.parametrize(
    ("sdists", "wheels", "expected"),
    [(0, 1, "fail"), (1, 1, "pass"), (2, 1, "fail"), (1, 0, "fail")],
)
def test_distribution_count(
    tmp_path: pathlib.Path, sdists: int, wheels: int, expected: str
) -> None:
    """Only a single sdist with at least one wheel is accepted."""
    workflow = yaml.load(
        (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8"),
        Loader=StrictLoader,  # ruff: ignore[unsafe-yaml-load] - strict SafeLoader subclass
    )
    steps = workflow["jobs"]["build-dist"]["steps"]
    script = next(
        step["run"]
        for step in steps
        if step.get("name") == "Require exactly one sdist to travel with the wheel"
    )
    dist = tmp_path / "dist"
    dist.mkdir()
    for index in range(sdists):
        (dist / f"package-{index}.tar.gz").touch()
    for index in range(wheels):
        (dist / f"package-{index}.whl").touch()
    result = subprocess.run(  # ruff: ignore[subprocess-without-shell-equals-true] - fixed argv
        [str(BASH), "-c", script],
        cwd=tmp_path,
        env={**os.environ, "WHEEL_DIR": "dist"},
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert (result.returncode == 0) is (expected == "pass"), result.stderr


@pytest.mark.parametrize("out_dir", ["custom", "release-dist"])
def test_action_stages_distributions_from_custom_output(
    tmp_path: pathlib.Path, out_dir: str
) -> None:
    """A custom build directory still uploads only distribution files."""
    action = yaml.load(
        (ROOT / ".github/actions/pure-python-wheel/action.yml").read_text(
            encoding="utf-8"
        ),
        Loader=StrictLoader,  # ruff: ignore[unsafe-yaml-load] - strict SafeLoader subclass
    )
    script = next(
        step["run"]
        for step in action["runs"]["steps"]
        if step.get("name") == "Stage distributions"
    )
    output = tmp_path / out_dir
    output.mkdir()
    (output / "package.tar.gz").touch()
    (output / "package.whl").touch()
    if out_dir != "release-dist":
        (output / "coverage.xml").touch()
    result = subprocess.run(  # ruff: ignore[subprocess-without-shell-equals-true] - fixed argv
        [str(BASH), "-c", script],
        cwd=tmp_path,
        env={**os.environ, "OUT_DIR": out_dir},
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    staged = {p.name for p in (tmp_path / "release-dist").iterdir()}
    assert staged == {"package.tar.gz", "package.whl"}, staged


def test_release_uploads_both_distribution_kinds(tmp_path: pathlib.Path) -> None:
    """The interim release step uploads both kinds from the named artefact."""
    workflow = yaml.load(
        (ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8"),
        Loader=StrictLoader,  # ruff: ignore[unsafe-yaml-load] - strict SafeLoader subclass
    )
    step = next(
        step
        for step in workflow["jobs"]["release"]["steps"]
        if step.get("name") == "Upload distributions to release"
    )
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "package.tar.gz").touch()
    (dist / "package.whl").touch()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    gh = bin_dir / "gh"
    gh.write_text('#!/bin/bash\nprintf "%s\\n" "$@" > args\n', encoding="utf-8")
    gh.chmod(0o755)
    result = subprocess.run(  # ruff: ignore[subprocess-without-shell-equals-true] - fixed argv
        [str(BASH), "-c", step["run"]],
        cwd=tmp_path,
        env={
            **os.environ,
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "GITHUB_REF_NAME": "v1.0.0",
            "BASH_ENV": "",
        },
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    arguments = (tmp_path / "args").read_text(encoding="utf-8").splitlines()
    assert "dist/package.tar.gz" in arguments, arguments
    assert "dist/package.whl" in arguments, arguments
