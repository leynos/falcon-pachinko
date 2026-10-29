"""Enforce project-lock use at repository command boundaries.

The scanner parses Make recipes and GitHub Actions ``run:`` scripts, including
shell command boundaries, prefixes and uv option arity. It flags project
``uv run`` and ``uv sync`` commands without ``--locked`` while leaving
``--no-project`` runs and isolated ``uv tool``, ``uvx`` and ``uv build``
environments outside the project lock. ``tests/test_uv_toolchain.py`` checks
lock tracking and contributor guidance.
"""

from __future__ import annotations

import pytest
import yaml
from hypothesis import given
from hypothesis import strategies as st

from tests._makefile import MAKEFILE, REPO_ROOT
from tests._uv_command_scanner import (
    ShellCommandSyntaxError,
    _run_step_scripts,
    _unlocked_project_commands,
)
from tests.workflow_contracts.strict_yaml import StrictLoader


def test_makefile_project_commands_require_the_lockfile() -> None:
    """Project commands in Make must reject a stale dependency lock."""
    try:
        violations = _unlocked_project_commands(
            MAKEFILE.read_text(encoding="utf-8"), is_makefile=True
        )
    except ValueError as error:
        message = f"Makefile: {error}"
        raise ShellCommandSyntaxError(message) from error

    assert not violations, "Makefile project commands must use --locked:\n" + "\n".join(
        f"Makefile:{line_number}: {command}" for line_number, command in violations
    )


def test_makefile_uv_command_variables_require_the_lockfile() -> None:
    """Make variable recipes must not hide an unlocked uv invocation."""
    source = "MUTMUT = UV_PYTHON=3.13 $(UV_ENV) $(UV) run --with mutmut mutmut"
    violations = _unlocked_project_commands(source, is_makefile=True)

    assert violations == [(1, source)], (
        "Make variable command definitions must enforce the project lock; "
        f"found {violations!r}"
    )


def test_ci_run_steps_require_the_project_lockfile() -> None:
    """Repository-owned CI run steps must reject stale project locks."""
    workflow_files = sorted((REPO_ROOT / ".github" / "workflows").glob("*.yml"))
    action_files = sorted((REPO_ROOT / ".github" / "actions").glob("*/action.yml"))
    violations: list[str] = []

    for path in [*workflow_files, *action_files]:
        source = path.read_text(encoding="utf-8")
        document = yaml.load(source, Loader=StrictLoader)  # ruff: ignore[unsafe-yaml-load] - strict SafeLoader subclass
        scripts = _run_step_scripts(document)
        for script in scripts:
            try:
                commands = _unlocked_project_commands(script)
            except ValueError as error:
                location = path.relative_to(REPO_ROOT)
                message = f"{location}: {error}"
                raise ShellCommandSyntaxError(message) from error
            for _, command in commands:
                violations.append(f"{path.relative_to(REPO_ROOT)}: {command}")

    assert not violations, (
        "CI project commands must use --locked (each entry names its file and "
        "offending run line):\n" + "\n".join(violations)
    )


@pytest.mark.parametrize(
    ("script", "expected"),
    [
        ("uv run --locked pytest && uv sync", [(1, "uv sync")]),
        (
            "uv run --no-project install.py && uv run pytest",
            [(1, "uv run pytest")],
        ),
        ("uv run --with mutmut --locked mutmut", []),
        ("uv run custom-cli --locked", [(1, "uv run custom-cli --locked")]),
        (
            "uv run custom-cli --no-project",
            [(1, "uv run custom-cli --no-project")],
        ),
        ("uv run --python 3.14 --no-project install.py", []),
        ("uv sync --group dev --locked", []),
        ("uv sync --group dev", [(1, "uv sync --group dev")]),
        ("uv --verbose run pytest", [(1, "uv --verbose run pytest")]),
        ("uv -vv run --locked pytest", []),
        ("uv --color never sync", [(1, "uv --color never sync")]),
        ("uv --project . sync --locked", []),
        ("@uv run pytest", [(1, "@uv run pytest")]),
        ("$(UV) run pytest", [(1, "$(UV) run pytest")]),
        ("@$(UV) run --locked pytest", []),
        (
            "env UV_PYTHON=3.13 uv run pytest",
            [(1, "env UV_PYTHON=3.13 uv run pytest")],
        ),
        ("env UV_PYTHON=3.13 uv run --locked pytest", []),
        (
            "env -u UV_CACHE_DIR uv sync",
            [(1, "env -u UV_CACHE_DIR uv sync")],
        ),
        (
            "env --unset UV_CACHE_DIR uv sync",
            [(1, "env --unset UV_CACHE_DIR uv sync")],
        ),
        ("env --ignore-environment --unset=UV_CACHE_DIR uv sync --locked", []),
        ("uv --no-cache-dir run pytest", [(1, "uv --no-cache-dir run pytest")]),
        ("uv --no-cache-dir run --locked pytest", []),
        ("uv tool run ruff && uvx ruff && uv build", []),
        ("echo ready # uv sync", []),
        ('echo "uv sync"', []),
    ],
)
def test_ci_command_contract_checks_each_shell_invocation(
    script: str, expected: list[tuple[int, str]]
) -> None:
    """Check project locks per invocation while exempting tool environments."""
    violations = _unlocked_project_commands(script)

    assert violations == expected, (
        "each project command must independently use --locked, while "
        "tooling and no-project calls stay outside the project lock; "
        f"expected {expected!r}, got {violations!r}"
    )


@given(
    command_prefix=st.sampled_from([
        (),
        ("env", "UV_CACHE_DIR=cache"),
        ("env", "-u", "UV_CACHE_DIR"),
        ("UV_PYTHON=3.13",),
    ]),
    global_options=st.lists(
        st.sampled_from([
            ("--no-cache-dir",),
            ("--cache-dir", "cache"),
            ("--verbose",),
        ]),
        max_size=3,
    ),
    run_options=st.lists(
        st.sampled_from([
            ("--extra", "examples"),
            ("--with", "mutmut"),
            ("--python", "3.13"),
        ]),
        max_size=3,
    ),
    separator=st.sampled_from(("&&", ";", "||")),
)
def test_command_scanner_finds_unlocked_run_across_supported_prefixes(
    command_prefix: tuple[str, ...],
    global_options: list[tuple[str, ...]],
    run_options: list[tuple[str, ...]],
    separator: str,
) -> None:
    """Supported prefixes and option arities must not hide an unlocked run."""
    command_tokens = [
        *command_prefix,
        "uv",
        *(token for option in global_options for token in option),
        "run",
        *(token for option in run_options for token in option),
        "pytest",
    ]
    unlocked_command = " ".join(command_tokens)
    script = f"uv run --locked pytest {separator} {unlocked_command}"

    assert _unlocked_project_commands(script) == [(1, unlocked_command)], (
        "the command scanner must find an unlocked uv run after supported "
        f"prefixes and option combinations in {script!r}"
    )


def test_command_scanner_skips_heredoc_bodies() -> None:
    """Heredoc text is data, and commands after its delimiter remain visible."""
    script = "cat <<'EOF'\nDon't run uv sync\nEOF\nuv run pytest"

    assert _unlocked_project_commands(script) == [(4, "uv run pytest")], (
        "heredoc content must not be tokenized as a shell command, and the "
        "scanner must continue after the closing delimiter"
    )


def test_command_scanner_keeps_multiline_quoted_text_as_one_argument() -> None:
    """Quoted text may span lines without hiding a later command."""
    script = 'python -c "\nuv sync\n" && uv run pytest'

    assert _unlocked_project_commands(script) == [(1, "uv run pytest")], (
        "a multiline quoted argument must remain data while following shell "
        "commands are still checked"
    )


def test_command_scanner_reports_unbalanced_quotes_with_the_source_line() -> None:
    """Malformed shell input must identify the line that could not be parsed."""
    with pytest.raises(ShellCommandSyntaxError, match="echo 'unfinished") as exc_info:
        _unlocked_project_commands("echo 'unfinished")

    assert "echo 'unfinished" in str(exc_info.value), (
        "an unbalanced-quote diagnostic must include the offending shell line"
    )
    assert "line 1" in str(exc_info.value), (
        "an unbalanced-quote diagnostic must include its logical line number"
    )
