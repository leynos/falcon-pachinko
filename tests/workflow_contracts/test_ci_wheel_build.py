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
        (ROOT / ".github/workflows/ci.yml").read_text(),
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
