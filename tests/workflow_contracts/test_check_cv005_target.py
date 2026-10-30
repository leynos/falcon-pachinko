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
import shlex
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
PYTEST_OPTIONS_WITH_ARGUMENTS = frozenset({
    "-k",
    "-m",
    "-o",
    "--confcutdir",
    "--deselect",
    "--ignore",
    "--ignore-glob",
    "--rootdir",
})

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


def _parse_pytest_command(command: str) -> tuple[list[str], list[str]] | None:
    """Split a shell command into its prefix and pytest arguments."""
    try:
        words = shlex.split(command)
    except ValueError:
        return None
    try:
        pytest_index = words.index("pytest")
    except ValueError:
        return None
    return words[:pytest_index], words[pytest_index + 1 :]


def _pytest_path_arguments(arguments: list[str]) -> list[str]:
    """Return positional pytest paths without option values."""
    paths = []
    index = 0
    while index < len(arguments):
        argument = arguments[index]
        if argument == "--":
            paths.extend(arguments[index + 1 :])
            break
        if argument in PYTEST_OPTIONS_WITH_ARGUMENTS:
            index += 2
        elif any(
            argument.startswith(f"{option}=")
            for option in PYTEST_OPTIONS_WITH_ARGUMENTS
        ) or argument.startswith("-"):
            index += 1
        else:
            paths.append(argument)
            index += 1
    return paths


def _pytest_command_ignores_paths(arguments: list[str]) -> bool:
    """Reject options that can exclude files from the complete test suite."""
    return any(
        argument == option or argument.startswith(f"{option}=")
        for argument in arguments
        for option in ("--ignore", "--ignore-glob", "--deselect")
    )


def _pytest_command_filters_by_keyword(arguments: list[str]) -> bool:
    """Reject keyword filters that can exclude tests from the complete suite."""
    index = 0
    while index < len(arguments):
        argument = arguments[index]
        if argument == "--":
            break
        if argument == "-k" or argument.startswith("-k"):
            return True
        if argument in PYTEST_OPTIONS_WITH_ARGUMENTS:
            index += 2
        else:
            index += 1
    return False


def _pytest_command_filters_by_marker(arguments: list[str]) -> bool:
    """Detect marker options that can narrow the local test target."""
    return any(argument == "-m" or argument.startswith("-m") for argument in arguments)


def _pytest_command_selects_complete_suite(command: str) -> bool:
    """Allow configured testpaths or both explicit roots, never one root."""
    parsed = _parse_pytest_command(command)
    if parsed is None:
        return False
    _, arguments = parsed
    if _pytest_command_filters_by_keyword(arguments):
        return False
    if _pytest_command_ignores_paths(arguments):
        return False
    return _pytest_path_arguments(arguments) in ([], ["falcon_pachinko", "tests"])


def _pytest_command_line(output: str) -> str | None:
    """Find a Make dry-run line that invokes pytest."""
    for line in output.splitlines():
        parsed = _parse_pytest_command(line)
        if parsed is None:
            continue
        prefix, _ = parsed
        if prefix[:2] == ["uv", "run"] and "which" not in prefix:
            return line
    return None


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


@pytest.mark.parametrize(
    "command",
    [
        pytest.param(
            'uv run --locked --extra examples pytest -v -m "not lint_toolchain"',
            id="configured-testpaths",
        ),
        pytest.param(
            "uv run --locked --extra examples pytest -v "
            'falcon_pachinko tests -m "not lint_toolchain"',
            id="both-explicit-roots",
        ),
    ],
)
def test_pytest_command_must_select_both_suites(command: str) -> None:
    """Explicit paths must retain both configured test roots."""
    assert _pytest_command_selects_complete_suite(command), (
        f"command must select the complete suite: {command!r}"
    )


@pytest.mark.parametrize(
    "command",
    [
        pytest.param(
            'uv run --locked --extra examples pytest -v tests -m "not lint_toolchain"',
            id="tests-only",
        ),
        pytest.param(
            "uv run --locked --extra examples pytest -v falcon_pachinko "
            '-m "not lint_toolchain"',
            id="package-only",
        ),
        pytest.param(
            "uv run --locked --extra examples pytest -v --ignore=tests "
            '-m "not lint_toolchain"',
            id="attached-ignore",
        ),
        pytest.param(
            "uv run --locked --extra examples pytest -v --ignore tests "
            '-m "not lint_toolchain"',
            id="separate-ignore",
        ),
        pytest.param(
            'uv run --locked --extra examples pytest -v --ignore-glob="tests/**" '
            '-m "not lint_toolchain"',
            id="ignore-glob",
        ),
        pytest.param(
            "uv run --locked --extra examples pytest -v --deselect "
            "tests/test_example.py::test_case -m 'not lint_toolchain'",
            id="deselect-separated",
        ),
        pytest.param(
            "uv run --locked --extra examples pytest -v "
            "--deselect=tests/test_example.py::test_case "
            "-m 'not lint_toolchain'",
            id="deselect-equals",
        ),
        pytest.param(
            'uv run --locked --extra examples pytest -v -k "selected tests" '
            '-m "not lint_toolchain"',
            id="keyword-filter-separated",
        ),
        pytest.param(
            "uv run --locked --extra examples pytest -v -k=selected-tests "
            '-m "not lint_toolchain"',
            id="keyword-filter-equals",
        ),
        pytest.param(
            "uv run --locked --extra examples pytest -v -kselected-tests "
            '-m "not lint_toolchain"',
            id="keyword-filter-compact",
        ),
    ],
)
def test_pytest_command_rejects_incomplete_selections(command: str) -> None:
    """Selection exclusions and keyword filters must not pass as complete suites."""
    assert not _pytest_command_selects_complete_suite(command), (
        f"command must not select the complete suite: {command!r}"
    )


@pytest.mark.parametrize(
    "arguments",
    [
        pytest.param(["-m", "smoke"], id="separated"),
        pytest.param(["-m=smoke"], id="equals"),
        pytest.param(["-msmoke"], id="compact"),
    ],
)
def test_pytest_marker_filter_is_detected(arguments: list[str]) -> None:
    """Marker selection can narrow the local target and must be detectable."""
    assert _pytest_command_filters_by_marker(arguments), (
        f"marker option must be detected: {arguments!r}"
    )


@pytest.mark.parametrize(
    "arguments",
    [
        pytest.param(["--maxfail=1"], id="long-option"),
        pytest.param(["-v"], id="no-marker-filter"),
    ],
)
def test_pytest_non_marker_options_are_not_marker_filters(
    arguments: list[str],
) -> None:
    """Other pytest options do not look like marker selection."""
    assert not _pytest_command_filters_by_marker(arguments), (
        f"non-marker options must not look like marker filters: {arguments!r}"
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
    local_command = _pytest_command_line(local.stdout)
    ci_command = _pytest_command_line(ci.stdout)
    assert local_command is not None, f"make test must run pytest: {local.stdout}"
    assert ci_command is not None, f"make test-ci must run pytest: {ci.stdout}"

    expected_prefix = ["uv", "run", "--locked", "--extra", "examples"]
    local_prefix, local_arguments = _parse_pytest_command(local_command) or ([], [])
    ci_prefix, ci_arguments = _parse_pytest_command(ci_command) or ([], [])
    assert local_prefix == ci_prefix == expected_prefix, (
        f"both test targets must use the locked examples environment: "
        f"local={local_prefix!r}, ci={ci_prefix!r}"
    )
    assert local_arguments, local_command
    assert local_arguments[0] == "-v", local_command
    assert not _pytest_command_filters_by_marker(local_arguments), (
        f"make test must not filter by pytest marker: {local_command}"
    )
    assert _pytest_command_selects_complete_suite(local_command), local_command
    assert ci_arguments[:3] == ["-v", "-m", "not lint_toolchain"], ci_command
    assert _pytest_command_selects_complete_suite(ci_command), ci_command
