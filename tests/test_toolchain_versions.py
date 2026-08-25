"""Contract tests keeping Makefile and CI toolchain pins in sync.

The Makefile pins ruff and ty via ``RUFF_VERSION`` and ``TY_VERSION`` while
the CI workflow installs the same tools with ``uv tool install <tool>==<v>``.
A version mismatch causes version-skew lint or typecheck failures without any
code change, so these tests assert both sites agree without hard-coding a
specific version.
"""

from __future__ import annotations

import pathlib
import re

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
MAKEFILE = REPO_ROOT / "Makefile"
CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"


def _makefile_pin(variable: str) -> str:
    """Extract a version pin variable from the Makefile."""
    text = MAKEFILE.read_text(encoding="utf-8")
    match = re.search(
        rf"^{re.escape(variable)}\s*\?=\s*(\S+)\s*$", text, flags=re.MULTILINE
    )
    if match is None:
        pytest.fail(f"Makefile does not define {variable}")
    return match.group(1)


def _ci_pin(tool: str) -> str:
    """Extract the pinned version a CI ``uv tool install`` step requests."""
    text = CI_WORKFLOW.read_text(encoding="utf-8")
    match = re.search(rf"uv tool install {re.escape(tool)}==(\S+)", text)
    if match is None:
        pytest.fail(f"ci.yml does not pin {tool} via 'uv tool install {tool}=='")
    return match.group(1)


@pytest.mark.parametrize(
    ("makefile_variable", "tool"),
    [("RUFF_VERSION", "ruff"), ("TY_VERSION", "ty")],
)
def test_makefile_and_ci_pin_same_version(makefile_variable: str, tool: str) -> None:
    """The Makefile pin and the CI pin must name the same release."""
    makefile_version = _makefile_pin(makefile_variable)
    ci_version = _ci_pin(tool)
    assert makefile_version == ci_version, (
        f"{tool} version pins have drifted: Makefile {makefile_variable} is "
        f"{makefile_version} but ci.yml installs {tool}=={ci_version}; "
        "bump both sites together"
    )
