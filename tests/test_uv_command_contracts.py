"""Enforce project uv lock use in Make and CI commands."""

from __future__ import annotations

import re
import shlex

import pytest
import yaml

from tests._makefile import MAKEFILE, REPO_ROOT

UV_PROJECT_COMMAND = re.compile(r"(?<![\w.-])(?:uv|\$\(UV\))[ \t]+(?:run|sync)\b")
MAKE_VARIABLE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
MAKE_VARIABLE_EXPANSION = re.compile(r"\$\((?!UV\))[A-Za-z_][A-Za-z0-9_]*\)")
SHELL_ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=")
SHELL_COMMAND_PREFIXES = frozenset({
    "command",
    "do",
    "else",
    "env",
    "exec",
    "if",
    "then",
    "!",
})
MAKE_ASSIGNMENT_OPERATORS = frozenset({"=", "?=", ":=", "+=", "!=", "::="})
SUBCOMMAND_OFFSET = 1
COMMAND_ARGUMENTS_OFFSET = 2
# Keep option arity aligned with `uv run --help`: their values must not be
# mistaken for the command whose arguments no longer belong to uv.
UV_RUN_OPTIONS_WITH_VALUES = frozenset({
    "--extra",
    "--no-extra",
    "--group",
    "--no-group",
    "--only-group",
    "--no-editable-package",
    "--env-file",
    "--with",
    "-w",
    "--with-editable",
    "--with-requirements",
    "--package",
    "--python-platform",
    "--index",
    "--default-index",
    "--index-url",
    "-i",
    "--extra-index-url",
    "--find-links",
    "-f",
    "--index-strategy",
    "--keyring-provider",
    "--upgrade-package",
    "-P",
    "--upgrade-group",
    "--resolution",
    "--prerelease",
    "--prerelease-package",
    "--fork-strategy",
    "--exclude-newer",
    "--exclude-newer-package",
    "--reinstall-package",
    "--link-mode",
    "--config-setting",
    "-C",
    "--config-settings-package",
    "--no-build-isolation-package",
    "--no-build-package",
    "--no-binary-package",
    "--cache-dir",
    "--refresh-package",
    "--python",
    "-p",
    "--allow-insecure-host",
    "--color",
    "--directory",
    "--project",
    "--config-file",
})


def _logical_lines(text: str) -> list[tuple[int, str]]:
    """Return source lines joined across shell or Make continuations.

    Returns
    -------
    list of tuple of int and str
        Logical lines paired with their one-based starting line numbers.
    """
    logical_lines: list[tuple[int, str]] = []
    fragments: list[str] = []
    start_line = 1

    for line_number, line in enumerate(text.splitlines(), start=1):
        if not fragments and line.lstrip().startswith("#"):
            continue
        if not fragments:
            start_line = line_number
        stripped = line.rstrip()
        continues = stripped.endswith("\\")
        fragments.append(stripped[:-1].strip() if continues else line.strip())
        if not continues:
            logical_lines.append((start_line, " ".join(fragments)))
            fragments = []

    if fragments:
        logical_lines.append((start_line, " ".join(fragments)))
    return logical_lines


def _run_step_scripts(document: object) -> list[str]:
    """Collect ``run:`` script values from a parsed workflow document."""
    scripts: list[str] = []

    def visit(value: object) -> None:
        match value:
            case dict():
                run_script = value.get("run")
                match run_script:
                    case str() as script:
                        scripts.append(script)
                    case _:
                        pass
                for child in value.values():
                    visit(child)
            case list():
                for child in value:
                    visit(child)
            case _:
                return

    visit(document)
    return scripts


def _shell_commands(line: str) -> list[list[str]]:
    """Split a logical shell line at command separators.

    Returns
    -------
    list of list of str
        Shell command token groups, with separators removed.
    """
    lexer = shlex.shlex(line, posix=True, punctuation_chars=";&|")
    lexer.whitespace_split = True
    commands: list[list[str]] = []
    current: list[str] = []

    for token in lexer:
        if token and all(character in ";&|" for character in token):
            if current:
                commands.append(current)
                current = []
        else:
            current.append(token)

    if current:
        commands.append(current)
    return commands


def _normalise_make_command(command: list[str], *, is_makefile: bool) -> list[str]:
    """Remove Make's recipe marker and variable assignment prefix."""
    tokens = command.copy()
    if tokens:
        tokens[0] = tokens[0].removeprefix("@")
    if not is_makefile:
        return tokens

    try:
        name, operator, *recipe = tokens
    except ValueError:
        return tokens
    if MAKE_VARIABLE.fullmatch(name) and operator in MAKE_ASSIGNMENT_OPERATORS:
        return recipe
    return tokens


def _uv_invocation_index(tokens: list[str], *, is_makefile: bool) -> int | None:
    """Return the token index of uv when it is the invoked executable."""
    invocation = 0
    while invocation < len(tokens) and _is_command_prefix(
        tokens[invocation], is_makefile=is_makefile
    ):
        invocation += 1

    subcommand_index = invocation + SUBCOMMAND_OFFSET
    if subcommand_index >= len(tokens):
        return None
    if tokens[invocation] not in {"uv", "$(UV)"}:
        return None
    if tokens[subcommand_index] not in {"run", "sync"}:
        return None
    return invocation


def _is_command_prefix(token: str, *, is_makefile: bool) -> bool:
    """Return whether a shell token precedes the invoked command."""
    return (
        token in SHELL_COMMAND_PREFIXES
        or SHELL_ASSIGNMENT.match(token) is not None
        or (is_makefile and MAKE_VARIABLE_EXPANSION.fullmatch(token) is not None)
    )


def _is_unlocked_project_command(command: list[str], *, is_makefile: bool) -> bool:
    """Return whether one project invocation omits its lock enforcement.

    Returns
    -------
    bool
        Whether the project command's uv option prefix omits ``--locked``.
    """
    tokens = _normalise_make_command(command, is_makefile=is_makefile)
    invocation = _uv_invocation_index(tokens, is_makefile=is_makefile)
    if invocation is None:
        return False

    subcommand = tokens[invocation + SUBCOMMAND_OFFSET]
    arguments = tokens[invocation + COMMAND_ARGUMENTS_OFFSET :]
    match subcommand:
        case "sync":
            return "--locked" not in arguments
        case "run":
            uv_options = _uv_run_options_before_command(arguments)
            return "--locked" not in uv_options and "--no-project" not in uv_options
        case _:
            return False


def _uv_run_options_before_command(arguments: list[str]) -> list[str]:
    """Return uv run options before its invoked command.

    Returns
    -------
    list of str
        Option names passed to uv before the command boundary.
    """
    options: list[str] = []
    index = 0

    while index < len(arguments):
        argument = arguments[index]
        if argument == "--":
            break
        if not argument.startswith("-"):
            break
        option = argument.partition("=")[0]
        options.append(option)
        takes_value = option in UV_RUN_OPTIONS_WITH_VALUES and "=" not in argument
        index += 2 if takes_value else 1

    return options


def _unlocked_project_commands(
    text: str, *, is_makefile: bool = False
) -> list[tuple[int, str]]:
    """Return project ``uv run`` or ``uv sync`` lines lacking ``--locked``."""
    return [
        (line_number, " ".join(command))
        for line_number, line in _logical_lines(text)
        if UV_PROJECT_COMMAND.search(line)
        for command in _shell_commands(line)
        if _is_unlocked_project_command(command, is_makefile=is_makefile)
    ]


def test_makefile_project_commands_require_the_lockfile() -> None:
    """Project commands in Make must reject a stale dependency lock."""
    violations = _unlocked_project_commands(
        MAKEFILE.read_text(encoding="utf-8"), is_makefile=True
    )

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
        scripts = _run_step_scripts(yaml.safe_load(source))
        for script in scripts:
            for _, command in _unlocked_project_commands(script):
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
        ("@uv run pytest", [(1, "@uv run pytest")]),
        ("$(UV) run pytest", [(1, "$(UV) run pytest")]),
        ("@$(UV) run --locked pytest", []),
        (
            "env UV_PYTHON=3.13 uv run pytest",
            [(1, "env UV_PYTHON=3.13 uv run pytest")],
        ),
        ("env UV_PYTHON=3.13 uv run --locked pytest", []),
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
