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

import pytest

REPOSITORY_ROOT = pathlib.Path(__file__).resolve().parents[2]
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
    assert {"output-path", "format", "with-ratchet"} <= set(config["selection"]), (
        "the publisher selection is incomplete"
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
