"""Exercise the checked-in distribution cardinality check."""

from __future__ import annotations

import shutil
import sys
import typing as typ

import pytest

from tests.helpers.ci_workflows import ROOT
from tests.helpers.release_workflow import run_bash, step_script
from tests.helpers.strict_yaml import load

if typ.TYPE_CHECKING:
    import pathlib

BASH = shutil.which("bash")
pytestmark = pytest.mark.skipif(
    sys.platform == "win32" or BASH is None, reason="The CI step requires Bash."
)


@pytest.mark.parametrize(
    ("sdists", "wheels", "expected"),
    [(0, 1, "fail"), (1, 1, "pass"), (2, 1, "fail"), (1, 0, "fail"), (1, 2, "fail")],
)
def test_distribution_count(
    tmp_path: pathlib.Path, sdists: int, wheels: int, expected: str
) -> None:
    """Only exactly one sdist and one wheel is accepted."""
    script = step_script(
        "build-dist",
        "Require exactly one sdist to travel with the wheel",
        workflow="ci.yml",
    )
    dist = tmp_path / "dist"
    dist.mkdir()
    for index in range(sdists):
        (dist / f"package-{index}.tar.gz").touch()
    for index in range(wheels):
        (dist / f"package-{index}.whl").touch()
    result = run_bash(script, tmp_path, {"WHEEL_DIR": "dist"})
    assert (result.returncode == 0) is (expected == "pass"), result.stderr


@pytest.mark.parametrize("out_dir", ["custom", "release-dist"])
def test_action_stages_distributions_from_custom_output(
    tmp_path: pathlib.Path, out_dir: str
) -> None:
    """A custom build directory still uploads only distribution files."""
    action_path = ROOT / ".github/actions/pure-python-wheel/action.yml"
    action = typ.cast(
        "dict[str, typ.Any]",
        load(action_path.read_text(encoding="utf-8"), str(action_path)),
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
    result = run_bash(script, tmp_path, {"OUT_DIR": out_dir})
    assert result.returncode == 0, result.stderr
    staged = {p.name for p in (tmp_path / "release-dist").iterdir()}
    assert staged == {"package.tar.gz", "package.whl"}, staged
