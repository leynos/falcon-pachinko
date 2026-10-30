"""Enforce project-lock use at repository command boundaries.

The scanner parses Make recipes and GitHub Actions ``run:`` scripts, including
shell command boundaries, prefixes and uv option arity. It flags project
``uv run`` and ``uv sync`` commands without ``--locked`` while leaving
``--no-project`` runs and isolated ``uv tool``, ``uvx`` and ``uv build``
environments outside the project lock. ``tests/test_uv_toolchain.py`` checks
lock tracking and contributor guidance.
"""

from __future__ import annotations

import typing as typ

import pytest
import yaml
from hypothesis import given
from hypothesis import strategies as st

if typ.TYPE_CHECKING:
    import pathlib

from tests._makefile import MAKEFILE, REPO_ROOT
from tests._shell_lines import ShellCommandSyntaxError, _logical_lines, _shell_commands
from tests._uv_command_scanner import (
    UnsupportedCommandOptionError,
    UnsupportedEnvOptionError,
    UnsupportedUvOptionError,
    _is_project_subcommand,
    run_step_scripts,
    unlocked_project_commands,
)
from tests.workflow_contracts.strict_yaml import load_strict_yaml


def test_makefile_project_commands_require_the_lockfile() -> None:
    """Project commands in Make must reject a stale dependency lock."""
    try:
        violations = unlocked_project_commands(
            MAKEFILE.read_text(encoding="utf-8"), is_makefile=True
        )
    except (ShellCommandSyntaxError, UnsupportedCommandOptionError) as error:
        message = f"Makefile: {error}"
        raise type(error)(message) from error

    assert not violations, "Makefile project commands must use --locked:\n" + "\n".join(
        f"Makefile:{line_number}: {command}" for line_number, command in violations
    )


def test_makefile_uv_command_variables_require_the_lockfile() -> None:
    """Make variable recipes must not hide an unlocked uv invocation."""
    source = "MUTMUT = UV_PYTHON=3.13 $(UV_ENV) $(UV) run --with mutmut mutmut"
    violations = unlocked_project_commands(source, is_makefile=True)

    assert violations == [(1, source)], (
        "Make variable command definitions must enforce the project lock; "
        f"found {violations!r}"
    )


def _ci_workflow_and_action_paths() -> list[pathlib.Path]:
    """Return repository-owned GitHub workflow and action YAML files."""
    workflows = REPO_ROOT / ".github" / "workflows"
    actions = REPO_ROOT / ".github" / "actions"
    return sorted([
        *workflows.glob("*.yml"),
        *workflows.glob("*.yaml"),
        *actions.glob("*/action.yml"),
        *actions.glob("*/action.yaml"),
    ])


def _strict_workflow_document(path: pathlib.Path) -> object:
    """Load a workflow while reporting YAML errors at their repository path."""
    source = path.read_text(encoding="utf-8")
    try:
        return load_strict_yaml(source)
    except yaml.YAMLError as error:
        location = path.relative_to(REPO_ROOT)
        message = f"{location} is not strictly parsable YAML: {error}"
        raise ValueError(message) from error


def _workflow_script_violations(path: pathlib.Path, script: str) -> list[str]:
    """Return unlocked commands with the workflow path in each diagnostic."""
    try:
        commands = unlocked_project_commands(script)
    except (ShellCommandSyntaxError, UnsupportedCommandOptionError) as error:
        location = path.relative_to(REPO_ROOT)
        message = f"{location}: {error}"
        raise type(error)(message) from error
    return [f"{path.relative_to(REPO_ROOT)}: {command}" for _, command in commands]


def _workflow_lock_violations(path: pathlib.Path) -> list[str]:
    """Check every Actions ``run:`` script in one workflow or action."""
    scripts = run_step_scripts(_strict_workflow_document(path))
    return [
        violation
        for script in scripts
        for violation in _workflow_script_violations(path, script)
    ]


def test_ci_run_steps_require_the_project_lockfile() -> None:
    """Repository-owned CI run steps must reject stale project locks."""
    violations = [
        violation
        for path in _ci_workflow_and_action_paths()
        for violation in _workflow_lock_violations(path)
    ]
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
        ("uv --native-tls sync", [(1, "uv --native-tls sync")]),
        ("uv --preview sync", [(1, "uv --preview sync")]),
        ("uv --isolated sync", [(1, "uv --isolated sync")]),
        (
            "uv --python-preference only-managed sync",
            [(1, "uv --python-preference only-managed sync")],
        ),
        (
            "uv --preview-features json-output sync",
            [(1, "uv --preview-features json-output sync")],
        ),
        ("uv --project . sync --locked", []),
        ("uv --version", []),
        ("uv -h", []),
        ("uv -- run pytest", [(1, "uv -- run pytest")]),
        (
            "uv run --preview-features json-output --locked pytest",
            [],
        ),
        (
            "uv run --python-preference only-managed --locked pytest",
            [],
        ),
        ("@uv run pytest", [(1, "@uv run pytest")]),
        ("$(UV) run pytest", []),
        ("@$(UV) run --locked pytest", []),
        ("{ uv sync; }", [(1, "{ uv sync")]),
        ("{ uv sync --locked; }", []),
        ("(cd sub && uv sync)", [(1, "uv sync")]),
        ("(uv sync)", [(1, "uv sync")]),
        ("x=$(uv sync)", [(1, "uv sync")]),
        ("echo '(uv sync)'", []),
        ("echo '(' uv sync", []),
        ('echo "$(( 2 + 2 ))"', []),
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
            "env -C subdir uv sync",
            [(1, "env -C subdir uv sync")],
        ),
        (
            "env --chdir subdir uv sync",
            [(1, "env --chdir subdir uv sync")],
        ),
        (
            "env --chdir=subdir uv sync",
            [(1, "env --chdir=subdir uv sync")],
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
        (
            "if [ $# -gt 0 ]; then uv sync; fi",
            [(1, "then uv sync")],
        ),
        ('echo "uv sync"', []),
    ],
)
def test_ci_command_contract_checks_each_shell_invocation(
    script: str, expected: list[tuple[int, str]]
) -> None:
    """Check project locks per invocation while exempting tool environments."""
    violations = unlocked_project_commands(script)

    assert violations == expected, (
        "each project command must independently use --locked, while "
        "tooling and no-project calls stay outside the project lock; "
        f"expected {expected!r}, got {violations!r}"
    )


@pytest.mark.parametrize(
    ("tokens", "index"),
    [
        (["run"], 0),
        (["sync"], 0),
        (["--locked", "run"], 1),
    ],
)
def test_project_subcommand_predicate_accepts_run_and_sync(
    tokens: list[str], index: int
) -> None:
    """The scanner recognises only project subcommands at valid indexes."""
    assert _is_project_subcommand(tokens, index), (
        f"run or sync must be recognised at index {index}: {tokens!r}"
    )


@pytest.mark.parametrize(
    ("tokens", "index"),
    [
        ([], 0),
        (["run"], -1),
        (["run"], 1),
        (["build"], 0),
    ],
)
def test_project_subcommand_predicate_rejects_other_tokens(
    tokens: list[str], index: int
) -> None:
    """The scanner rejects unsupported or out-of-range subcommand tokens."""
    assert not _is_project_subcommand(tokens, index), (
        f"non-project subcommand must be rejected at index {index}: {tokens!r}"
    )


@pytest.mark.parametrize(
    "recipe_prefix",
    [
        "@",
        "-",
        "+",
        "@-",
        "-@",
        "@+",
        "+@",
        "-+",
        "+-",
        "@-+",
        "@+-",
        "-@+",
        "-+@",
        "+@-",
        "+-@",
    ],
)
def test_make_recipe_prefixes_do_not_hide_unlocked_uv_commands(
    recipe_prefix: str,
) -> None:
    """Make's ignore, silent and recursive prefixes may appear in combination."""
    unlocked_recipe = f"{recipe_prefix}uv sync"
    locked_recipe = f"{recipe_prefix}uv sync --locked"

    assert unlocked_project_commands(unlocked_recipe, is_makefile=True) == [
        (1, unlocked_recipe)
    ], "all Make recipe prefix combinations must leave the uv command visible"
    assert not unlocked_project_commands(locked_recipe, is_makefile=True), (
        "a locked uv command must pass with any valid Make recipe prefix"
    )


@pytest.mark.parametrize(
    ("trailing_backslash_count", "expected"),
    [
        (1, []),
        (2, [(2, "uv run pytest")]),
        (3, []),
    ],
)
def test_command_scanner_uses_odd_backslashes_for_line_continuation(
    trailing_backslash_count: int, expected: list[tuple[int, str]]
) -> None:
    """Only an odd final backslash escapes the physical line boundary."""
    trailing_backslashes = "\\" * trailing_backslash_count
    script = f"echo path{trailing_backslashes}\nuv run pytest"

    assert unlocked_project_commands(script) == expected, (
        "an even run of trailing backslashes must leave the following command "
        "at a new shell boundary"
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

    assert unlocked_project_commands(script) == [(1, unlocked_command)], (
        "the command scanner must find an unlocked uv run after supported "
        f"prefixes and option combinations in {script!r}"
    )


def test_command_scanner_skips_heredoc_bodies() -> None:
    """Heredoc text is data, and commands after its delimiter remain visible."""
    script = "cat <<'EOF'\nDon't run uv sync\nEOF\nuv run pytest"

    assert unlocked_project_commands(script) == [(4, "uv run pytest")], (
        "heredoc content must not be tokenized as a shell command, and the "
        "scanner must continue after the closing delimiter"
    )


def test_shell_logical_lines_preserve_source_locations_after_heredocs() -> None:
    """Heredoc bodies leave the next executable line's source location intact."""
    script = "cat <<'EOF'\nDon't run uv sync\nEOF\nuv run pytest"

    assert _logical_lines(script) == [(1, "cat <<'EOF'"), (4, "uv run pytest")], (
        "logical shell lines must skip heredoc data and retain physical locations"
    )


def test_shell_command_groups_preserve_quoted_parentheses() -> None:
    """Quoted grouping characters remain argument data at command boundaries."""
    commands = _shell_commands(
        "echo '(uv sync)' && uv sync", line_number=7, is_makefile=False
    )

    assert commands == [["echo", "(uv sync)"], ["uv", "sync"]], (
        "quoted parentheses must remain in one argument while shell separators split"
    )


def test_command_scanner_rejects_an_unterminated_heredoc() -> None:
    """An unterminated heredoc must not hide later project commands."""
    script = "cat <<EOF\nuv sync"

    with pytest.raises(ShellCommandSyntaxError, match=r"line 1.*EOF"):
        unlocked_project_commands(script)


def test_command_scanner_does_not_treat_arithmetic_shift_as_a_heredoc() -> None:
    """A shift operator inside arithmetic expansion does not start a heredoc."""
    script = "value=$(( 1 << 2 ))\nuv sync"

    assert unlocked_project_commands(script) == [(2, "uv sync")], (
        "the arithmetic shift must leave the following shell command visible"
    )


def test_command_scanner_does_not_treat_here_strings_as_heredocs() -> None:
    """A shell here-string must not hide the following project command."""
    script = "cat <<< 'uv sync'\nuv sync"

    assert unlocked_project_commands(script) == [(2, "uv sync")], (
        "here-string input is not a heredoc body, so the next command must be "
        "checked for --locked"
    )


def test_command_scanner_fails_closed_on_unknown_uv_global_options() -> None:
    """An unrecognized global option must not hide a project subcommand."""
    script = "echo ready\nuv --unrecognised sync"

    with pytest.raises(UnsupportedUvOptionError, match=r"line 2.*uv --unrecognised"):
        unlocked_project_commands(script)


def test_command_scanner_fails_closed_on_unsupported_env_options() -> None:
    """An env option the scanner cannot parse must not hide the wrapped uv."""
    script = "env -S 'uv sync'"

    with pytest.raises(UnsupportedEnvOptionError, match=r"line 1.*env -S"):
        unlocked_project_commands(script)


def test_command_scanner_fails_closed_on_quoted_command_substitutions() -> None:
    """A command substitution in double quotes must not hide a project call."""
    script = 'x="$(uv sync)"'

    with pytest.raises(
        ShellCommandSyntaxError,
        match=r"Unsupported quoted command substitution at shell line 1",
    ):
        unlocked_project_commands(script)


def test_command_scanner_allows_nested_quotes_in_path_substitutions() -> None:
    """Quoted paths inside ordinary substitutions are valid shell syntax."""
    script = (
        'if [ "$(realpath "$OUT_DIR")" != "$(realpath release-dist)" ]; '
        'then cp -- "${files[@]}" release-dist/; fi'
    )

    assert not unlocked_project_commands(script), (
        "safe path substitutions must not prevent scanning the surrounding run step"
    )


def test_makefile_scanner_fails_closed_on_quoted_shell_substitutions() -> None:
    """Escaped Make dollars identify shell substitutions after Make expands."""
    source = 'TARGET = "$$(uv sync)"'

    with pytest.raises(
        ShellCommandSyntaxError,
        match=r"Unsupported quoted command substitution at shell line 1",
    ):
        unlocked_project_commands(source, is_makefile=True)


def test_command_scanner_keeps_multiline_quoted_text_as_one_argument() -> None:
    """Quoted text may span lines without hiding a later command."""
    script = 'python -c "\nuv sync\n" && uv run pytest'

    assert unlocked_project_commands(script) == [(1, "uv run pytest")], (
        "a multiline quoted argument must remain data while following shell "
        "commands are still checked"
    )


def test_command_scanner_reports_unbalanced_quotes_with_the_source_line() -> None:
    """Malformed shell input must identify the line that could not be parsed."""
    with pytest.raises(ShellCommandSyntaxError, match="echo 'unfinished") as exc_info:
        unlocked_project_commands("echo 'unfinished")

    assert "echo 'unfinished" in str(exc_info.value), (
        "an unbalanced-quote diagnostic must include the offending shell line"
    )
    assert "line 1" in str(exc_info.value), (
        "an unbalanced-quote diagnostic must include its logical line number"
    )
