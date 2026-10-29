"""The ``make check-cv005`` command, held at its boundary.

The CV-005 rules belong to the shared library, whose suite proves them. What
this repository owns is the wiring: the pinned full commit, the Python the
library runs under, the command line it receives, the parameters file it loads
and whether a failure reaches ``make``'s exit status. The real pinned command
runs in CI as the end-to-end check; these tests read the target without the
network.

Run with:

    uv run pytest tests/workflow_contracts
"""

from __future__ import annotations

import pathlib
import re
import shutil
import subprocess  # ruff: ignore[suspicious-subprocess-import] - the tests drive make itself
import tomllib
import typing as typ

import pytest

from .workflow_support import REPOSITORY

REPOSITORY_ROOT = pathlib.Path(__file__).resolve().parents[2]
PUBLISHER_SELECTION: typ.Final[dict[str, str]] = {
    "output-path": "coverage.xml",
    "format": "cobertura",
    "pytest-workers": "",
    "with-ratchet": "true",
}
MAKE = shutil.which("make")
FULL_COMMIT = re.compile(r"[0-9a-f]{40}")

pytestmark = pytest.mark.skipif(MAKE is None, reason="make is not installed")


def _make(*args: str) -> subprocess.CompletedProcess[str]:
    assert MAKE is not None, "make is required"
    return subprocess.run(  # ruff: ignore[subprocess-without-shell-equals-true] - fixed argv, no shell
        [MAKE, "--no-print-directory", "--silent", *args],
        cwd=REPOSITORY_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


def test_the_target_runs_the_check_from_a_full_commit_under_python_313() -> None:
    """Scenario: the target is expanded without running it.

    Invariant: the command names a 40-hex commit, keeps the ``#subdirectory``
    fragment, pins Python 3.13 and checks the repository root.
    """
    result = _make("-n", "check-cv005")

    assert result.returncode == 0, result.stderr
    command = result.stdout
    assert "--python 3.13" in command, "the library must run under Python 3.13"
    assert re.search(
        r"git\+https://github\.com/leynos/shared-actions@"
        r"[0-9a-f]{40}#subdirectory=packages/cv005-contracts",
        command,
    ), command
    assert command.rstrip().endswith("cv005-contracts check --repository ."), command


def test_the_pin_can_be_overridden_but_only_by_a_full_commit() -> None:
    """Scenario: the reference is replaced on the command line.

    Invariant: the replacement reaches the command, so a pin bump is one edit.
    """
    commit = "0123456789abcdef0123456789abcdef01234567"
    assert FULL_COMMIT.fullmatch(commit), "the fixture must be a full commit"

    result = _make("-n", "check-cv005", f"CV005_CONTRACTS_REF={commit}")

    assert f"shared-actions@{commit}#subdirectory" in result.stdout, result.stdout


def test_the_parameters_file_names_this_repository_and_python_313() -> None:
    """Scenario: the library's parameters are loaded from ``.github/cv005.toml``.

    Invariant: the file parses and carries the repository, the interpreter and
    the publisher selection the library compares with the lanes.
    """
    config = tomllib.loads(
        (REPOSITORY_ROOT / ".github" / "cv005.toml").read_text(encoding="utf-8")
    )

    assert config["repository"] == "leynos/falcon-pachinko", "wrong repository"
    assert config["interpreter"] == "3.13", "wrong interpreter"
    assert config["selection"] == PUBLISHER_SELECTION, (
        "the publisher selection must match the lanes' generate-coverage inputs"
    )


def test_the_command_receives_the_check_arguments(tmp_path: pathlib.Path) -> None:
    """Scenario: the tool is replaced by a stub that records its arguments.

    Invariant: the target passes ``check --repository .`` to the tool.
    """
    stub = tmp_path / "stub.sh"
    stub.write_text('#!/bin/sh\nprintf "%s\\n" "$@"\n', encoding="utf-8")
    stub.chmod(0o755)

    result = _make("check-cv005", f"CV005_CONTRACTS={stub}")

    assert result.returncode == 0, result.stderr
    assert result.stdout.split() == ["check", "--repository", "."], result.stdout


def test_a_failing_check_fails_the_target() -> None:
    """Scenario: the tool exits non-zero.

    Invariant: ``make check-cv005`` stops with a non-zero status rather than
    swallowing it.
    """
    result = _make("check-cv005", "CV005_CONTRACTS=false")

    assert result.returncode != 0, "the failure must propagate"


def test_the_workflow_contract_target_runs_the_shared_check_first() -> None:
    """Scenario: ``test-workflow-contracts`` is expanded with a marker tool.

    Invariant: the shared check runs, with its arguments, before the
    repository's own contracts, so dropping the prerequisite is caught.
    """
    result = _make("-n", "test-workflow-contracts", "CV005_CONTRACTS=SHARED_CHECK")

    lines = result.stdout.splitlines()
    shared = [i for i, line in enumerate(lines) if line.startswith("SHARED_CHECK")]
    local = [
        i for i, line in enumerate(lines) if "pytest tests/workflow_contracts" in line
    ]
    assert shared, f"the shared check must run: {result.stdout}"
    assert lines[shared[0]] == "SHARED_CHECK check --repository .", lines[shared[0]]
    assert local, f"the local contracts must run: {result.stdout}"
    assert shared[0] < local[0], "the shared check must run before the local contracts"


def test_the_lane_runs_the_check_as_its_own_step() -> None:
    """Scenario: ``ci.yml`` is read for the dedicated CV-005 step.

    Invariant: the pull-request lane has a step running ``make check-cv005``,
    because ``make test`` does not.
    """
    steps = REPOSITORY.jobs("ci.yml")["lint-test"].get("steps")

    assert isinstance(steps, list), "ci.yml:lint-test must declare its steps"
    runs = [step.get("run") for step in steps if isinstance(step, dict)]
    assert "make check-cv005" in runs, "the lane must run make check-cv005"


def test_the_lane_runs_ci_tests_before_cv005() -> None:
    """Scenario: ``ci.yml`` runs the remaining suite after lint-toolchain.

    Invariant: the locked ``make test-ci`` target runs before the CV-005 check.
    """
    steps = REPOSITORY.jobs("ci.yml")["lint-test"].get("steps")

    assert isinstance(steps, list), "ci.yml:lint-test must declare its steps"
    workflow_steps = [step for step in steps if isinstance(step, dict)]
    named_commands = [(step.get("name"), step.get("run")) for step in workflow_steps]
    test_step = ("Run tests", "make test-ci")
    cv005_step = ("Check the CV-005 contracts", "make check-cv005")
    assert test_step in named_commands, "the lane must run the CI test target"
    assert cv005_step in named_commands, "the lane must run the CV-005 check"
    assert named_commands.index(test_step) < named_commands.index(cv005_step), (
        "the CI test target must run before the CV-005 check"
    )
    required_steps = [
        workflow_steps[named_commands.index(command)]
        for command in (test_step, cv005_step)
    ]
    for step, (name, _) in zip(required_steps, (test_step, cv005_step), strict=True):
        assert "if" not in step, f"the {name!r} step must run unconditionally"
        assert "continue-on-error" not in step, (
            f"the {name!r} step must be able to fail"
        )


def test_local_and_ci_test_targets_keep_their_distinct_selections() -> None:
    """Scenario: Make exposes complete local and post-marker CI test targets.

    Invariant: ``test`` includes every test, while ``test-ci`` excludes only
    the ``lint_toolchain`` marker already run as a dedicated CI step.
    """
    local = _make("-n", "test")
    ci = _make("-n", "test-ci")

    assert local.returncode == 0, f"make -n test must succeed: {local.stderr}"
    assert ci.returncode == 0, f"make -n test-ci must succeed: {ci.stderr}"
    full_command = "uv run --locked --extra examples pytest -v"
    filtered_command = f'{full_command} -m "not lint_toolchain"'

    assert full_command in local.stdout, (
        f"make test must run the full locked suite: {local.stdout}"
    )
    assert "lint_toolchain" not in local.stdout, (
        f"make test must not exclude the lint_toolchain marker: {local.stdout}"
    )
    assert filtered_command in ci.stdout, (
        f"make test-ci must exclude only the lint_toolchain marker: {ci.stdout}"
    )
