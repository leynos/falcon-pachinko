"""Parse project commands for the uv toolchain contract tests."""

from __future__ import annotations

import re
import shlex

UV_PROJECT_COMMAND = re.compile(r"(?<![\w.-])(?:uv|\$\(UV\))[ \t]+(?:run|sync)\b")
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


def _shell_commands(line: str) -> list[str]:
    """Split a logical shell line at command separators.

    Returns
    -------
    list of str
        Shell command segments, without the separators between them.
    """
    lexer = shlex.shlex(line, posix=True, punctuation_chars=";&|")
    lexer.whitespace_split = True
    commands: list[str] = []
    current: list[str] = []

    for token in lexer:
        if token and all(character in ";&|" for character in token):
            if current:
                commands.append(" ".join(current))
                current = []
        else:
            current.append(token)

    if current:
        commands.append(" ".join(current))
    return commands


def _is_unlocked_project_command(command: str) -> bool:
    """Return whether one project invocation omits its lock enforcement.

    Returns
    -------
    bool
        Whether the project command's uv option prefix omits ``--locked``.
    """
    if UV_PROJECT_COMMAND.search(command) is None:
        return False

    tokens = shlex.split(command)
    if tokens:
        tokens[0] = tokens[0].removeprefix("@")
    invocation = next(
        (
            index
            for index in range(len(tokens) - 1)
            if tokens[index] in {"uv", "$(UV)"} and tokens[index + 1] in {"run", "sync"}
        ),
        None,
    )
    if invocation is None:
        return False

    subcommand = tokens[invocation + 1]
    arguments = tokens[invocation + 2 :]
    if subcommand == "sync":
        return "--locked" not in arguments

    uv_options = _uv_run_options_before_command(arguments)
    return "--locked" not in uv_options and "--no-project" not in uv_options


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


def _unlocked_project_commands(text: str) -> list[tuple[int, str]]:
    """Return project ``uv run`` or ``uv sync`` lines lacking ``--locked``."""
    return [
        (line_number, command)
        for line_number, line in _logical_lines(text)
        if UV_PROJECT_COMMAND.search(line)
        for command in _shell_commands(line)
        if _is_unlocked_project_command(command)
    ]
