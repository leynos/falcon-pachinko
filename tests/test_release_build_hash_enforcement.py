"""Prove the isolated release build enforces the constraint hashes.

The fast contracts in ``tests/test_release_build_inputs.py`` check that the
recipe passes the constraint and integrity flags. This test runs the real
``uv build`` with those same flags twice: once with the committed constraints
as a control, and once with every permitted hash for one build requirement
replaced. The tampered build must fail hash verification and write no
artefact, so a constraint that is not actually enforced cannot pass quietly.

The test is opt-in because ``--no-cache`` makes both builds download the
pinned backend again. CI's release-build security step sets
``RELEASE_BUILD_HASH_TEST=1``; a local ``make test`` skips it.
"""

from __future__ import annotations

import os
import re
import subprocess  # ruff: ignore[suspicious-subprocess-import] - runs the fixed release build
import typing as typ

import pytest

from scripts import release_wheel

if typ.TYPE_CHECKING:
    import pathlib

pytestmark = pytest.mark.skipif(
    os.environ.get("RELEASE_BUILD_HASH_TEST") != "1",
    reason="set RELEASE_BUILD_HASH_TEST=1 to run the real build",
)

_BUILD_TIMEOUT_SECONDS = 600
_TAMPERED_REQUIREMENT = "wheel=="
_HASH_PATTERN = re.compile(r"(--hash=sha256:)[0-9a-f]{64}")


def _tampered_constraints(source: pathlib.Path, target: pathlib.Path) -> None:
    """Write ``source`` with every permitted hash for ``wheel`` replaced.

    Parameters
    ----------
    source : pathlib.Path
        The committed constraints file.
    target : pathlib.Path
        Where to write the tampered copy.
    """
    tampered: list[str] = []
    in_requirement = False
    replaced = 0
    for line in source.read_text(encoding="utf-8").splitlines(keepends=True):
        if line.startswith(_TAMPERED_REQUIREMENT):
            in_requirement = True
        elif in_requirement and not line.startswith((" ", "\t")):
            in_requirement = False
        if in_requirement and _HASH_PATTERN.search(line):
            replaced += 1
            # Bind the suffix now: the lambda must not read the loop variable
            # later, and every replaced hash must stay distinct.
            suffix = f"{replaced:064x}"
            line = _HASH_PATTERN.sub(
                lambda match, suffix=suffix: f"{match.group(1)}{suffix}", line
            )
        tampered.append(line)
    assert replaced, "the constraints must list hashes for the wheel requirement"
    target.write_text("".join(tampered), encoding="utf-8")


def _build(
    constraints: pathlib.Path, output_dir: pathlib.Path
) -> subprocess.CompletedProcess[str]:
    """Run one isolated hash-verified sdist build into ``output_dir``."""
    output_dir.mkdir(parents=True)
    return subprocess.run(  # ruff: ignore[subprocess-without-shell-equals-true] - fixed uv command
        release_wheel.build_command(
            "--sdist", "--out-dir", str(output_dir), constraints=str(constraints)
        ),
        cwd=release_wheel.REPO_ROOT,
        env={**os.environ, "SOURCE_DATE_EPOCH": release_wheel.source_date_epoch()},
        capture_output=True,
        text=True,
        timeout=_BUILD_TIMEOUT_SECONDS,
        check=False,
    )


def test_the_isolated_build_rejects_a_tampered_constraint(
    tmp_path: pathlib.Path,
) -> None:
    """A hash the constraints do not permit fails the build after a control."""
    committed = release_wheel.REPO_ROOT / release_wheel.DEFAULT_CONSTRAINTS
    release_wheel.clear_build_state()
    control = _build(committed, tmp_path / "control")
    constraints = tmp_path / "tampered-constraints.txt"
    _tampered_constraints(committed, constraints)
    refused = _build(constraints, tmp_path / "tampered")
    release_wheel.clear_build_state()

    assert control.returncode == 0, control.stderr
    assert list((tmp_path / "control").glob("*.tar.gz")), (
        "the control build must prove the command succeeds as configured"
    )
    assert refused.returncode != 0, "a tampered hash must fail the build"
    output = f"{refused.stdout}\n{refused.stderr}".lower()
    assert "hash mismatch" in output, "the failure must be the hash check"
    assert _TAMPERED_REQUIREMENT in output, (
        "the failure must name the tampered requirement"
    )
    assert not list((tmp_path / "tampered").glob("*.tar.gz")), (
        "a refused build must not write a distribution"
    )
