"""Parse project ``uv`` commands for the repository lock contract tests."""

from __future__ import annotations

import re
import shlex


class ShellCommandSyntaxError(ValueError):
    """A shell command could not be parsed with source-line context."""


MAKE_VARIABLE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
MAKE_VARIABLE_EXPANSION = re.compile(r"\$\((?!UV\))[A-Za-z_][A-Za-z0-9_]*\)")
SHELL_ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=")
SHELL_COMMAND_PREFIXES = frozenset({
    "command",
    "do",
    "else",
    "exec",
    "if",
    "then",
    "!",
})
ENV_FLAGS = frozenset({
    "-",
    "-0",
    "--debug",
    "--ignore-environment",
    "--null",
    "-i",
    "-v",
})
ENV_OPTIONS_WITH_VALUES = frozenset({"-u", "--unset"})
MAKE_ASSIGNMENT_OPERATORS = frozenset({"=", "?=", ":=", "+=", "!=", "::="})
SHELL_HEREDOC_PART = re.compile(
    r"(?P<comment>(?<![^\s;&|])\#[^\n]*)|'[^']*'|"
    r'"(?:\\.|[^"\\])*"|\\.|(?P<operator><<-(?!<)|<<(?!<))'
)
# Keep global option arity aligned with `uv --help`; their values must not be
# mistaken for the command whose arguments no longer belong to uv.
UV_GLOBAL_OPTIONS_WITH_VALUES = frozenset({
    "--allow-insecure-host",
    "--cache-dir",
    "--color",
    "--config-file",
    "--directory",
    "--project",
})
UV_GLOBAL_FLAGS = frozenset({
    "--managed-python",
    "--no-cache",
    "--no-cache-dir",
    "--no-config",
    "--no-managed-python",
    "--no-progress",
    "--no-python-downloads",
    "--offline",
    "--quiet",
    "--system-certs",
    "--verbose",
    "-n",
})
UV_REPEATED_OUTPUT_OPTIONS = re.compile(r"^-(?:q+|v+)$")
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
    pending_heredocs: list[tuple[str, bool]] = []
    start_line = 1

    for line_number, line in enumerate(text.splitlines(), start=1):
        if _skip_shell_line(line, fragments, pending_heredocs):
            continue
        if not fragments:
            start_line = line_number
        if not _append_shell_fragment(line, fragments):
            _finish_logical_line(fragments, start_line, logical_lines, pending_heredocs)

    if fragments:
        logical_lines.append((start_line, " ".join(fragments)))
    return logical_lines


def _skip_shell_line(
    line: str, fragments: list[str], pending_heredocs: list[tuple[str, bool]]
) -> bool:
    """Return whether the physical line is a comment or heredoc body."""
    if _consume_heredoc_line(line, pending_heredocs):
        return True
    return not fragments and line.lstrip().startswith("#")


def _append_shell_fragment(line: str, fragments: list[str]) -> bool:
    """Append a physical line and report whether its shell continuation remains open."""
    stripped = line.rstrip()
    continues = stripped.endswith("\\")
    fragments.append(stripped[:-1].strip() if continues else line.strip())
    return continues


def _finish_logical_line(
    fragments: list[str],
    start_line: int,
    logical_lines: list[tuple[int, str]],
    pending_heredocs: list[tuple[str, bool]],
) -> None:
    """Store a complete shell line or retain it while a quote remains open."""
    logical_line = " ".join(fragments)
    if _has_incomplete_shell_quote(logical_line):
        return
    logical_lines.append((start_line, logical_line))
    pending_heredocs.extend(_heredoc_delimiters(logical_line))
    fragments.clear()


def _shell_tokens(line: str, *, punctuation_chars: str) -> list[str]:
    """Tokenize shell input with the punctuation used by the caller."""
    lexer = shlex.shlex(line, posix=True, punctuation_chars=punctuation_chars)
    lexer.whitespace_split = True
    return list(lexer)


def _has_incomplete_shell_quote(line: str) -> bool:
    """Return whether another physical line could close an open shell quote."""
    try:
        _shell_tokens(line, punctuation_chars=";&|<>")
    except ValueError as error:
        return str(error) == "No closing quotation"
    return False


def _heredoc_delimiters(line: str) -> list[tuple[str, bool]]:
    """Return heredoc delimiters and whether their bodies strip leading tabs."""
    delimiters: list[tuple[str, bool]] = []
    for match in SHELL_HEREDOC_PART.finditer(line):
        if match.group("comment") is not None:
            break
        operator = match.group("operator")
        if operator is None:
            continue
        delimiter = _first_shell_word(line[match.end() :])
        if delimiter is not None:
            delimiters.append((delimiter, operator.endswith("-")))
    return delimiters


def _first_shell_word(source: str) -> str | None:
    """Return the first shell word from the text following an operator."""
    lexer = shlex.shlex(source, posix=True, punctuation_chars=";&|<>")
    lexer.whitespace_split = True
    word = lexer.get_token()
    return word if isinstance(word, str) else None


def _consume_heredoc_line(line: str, pending_heredocs: list[tuple[str, bool]]) -> bool:
    """Consume heredoc body text, dropping a delimiter when it closes."""
    if not pending_heredocs:
        return False
    delimiter, strip_tabs = pending_heredocs[0]
    candidate = line.lstrip("\t") if strip_tabs else line
    if candidate == delimiter:
        pending_heredocs.pop(0)
    return True


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


def _shell_commands(line: str, *, line_number: int) -> list[list[str]]:
    """Split a logical shell line at command separators.

    Returns
    -------
    list of list of str
        Shell command token groups, with separators removed.
    """
    tokens = _shell_command_tokens(line, line_number=line_number)
    return _commands_from_tokens(tokens)


def _shell_command_tokens(line: str, *, line_number: int) -> list[str]:
    """Tokenize a shell line and attach its source location on syntax errors.

    Returns
    -------
    list of str
        Tokenized shell words.

    Raises
    ------
    ShellCommandSyntaxError
        The logical shell line has unbalanced quoting.
    """
    try:
        return _shell_tokens(line, punctuation_chars=";&|")
    except ValueError as error:
        message = f"Unbalanced quote at shell line {line_number}: {line!r}"
        raise ShellCommandSyntaxError(message) from error


def _commands_from_tokens(tokens: list[str]) -> list[list[str]]:
    """Group shell tokens by command separators, omitting the separators."""
    commands: list[list[str]] = []
    current: list[str] = []

    for token in tokens:
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


def _uv_subcommand_index(tokens: list[str], *, is_makefile: bool) -> int | None:
    """Return the run or sync index after uv and its global options."""
    invocation = _uv_executable_index(tokens, is_makefile=is_makefile)
    if invocation is None:
        return None
    return _uv_project_subcommand_index(tokens, invocation + 1)


def _uv_executable_index(tokens: list[str], *, is_makefile: bool) -> int | None:
    """Return the uv token after shell prefixes, if present."""
    invocation = 0
    while invocation < len(tokens):
        prefix_word = tokens[invocation]
        if prefix_word == "env":
            invocation = _env_command_index(tokens, invocation)
            if invocation is None:
                return None
            continue
        if not _is_command_prefix(prefix_word, is_makefile=is_makefile):
            break
        invocation += 1
    if invocation < len(tokens) and tokens[invocation] in {"uv", "$(UV)"}:
        return invocation
    return None


def _uv_project_subcommand_index(tokens: list[str], index: int) -> int | None:
    """Return the project subcommand after recognized uv global options."""
    subcommand_index = index
    while subcommand_index < len(tokens):
        option = tokens[subcommand_index]
        if not option.startswith("-"):
            break
        option_arity = _uv_global_option_arity(option)
        if option_arity is None:
            return None
        subcommand_index += option_arity

    if subcommand_index < len(tokens) and tokens[subcommand_index] in {"run", "sync"}:
        return subcommand_index
    return None


def _uv_global_option_arity(option: str) -> int | None:
    """Return how many tokens a recognized global option consumes."""
    name, separator, _ = option.partition("=")
    if name in UV_GLOBAL_OPTIONS_WITH_VALUES:
        return 1 if separator else 2
    if not separator and name in UV_GLOBAL_FLAGS:
        return 1
    if UV_REPEATED_OUTPUT_OPTIONS.fullmatch(option):
        return 1
    return None


def _is_command_prefix(token: str, *, is_makefile: bool) -> bool:
    """Return whether a shell token precedes the invoked command."""
    return (
        token in SHELL_COMMAND_PREFIXES
        or SHELL_ASSIGNMENT.match(token) is not None
        or (is_makefile and MAKE_VARIABLE_EXPANSION.fullmatch(token) is not None)
    )


ENV_OPTION_ARITY = {
    **dict.fromkeys(ENV_FLAGS, 1),
    **dict.fromkeys(ENV_OPTIONS_WITH_VALUES, 2),
}
ENV_INLINE_UNSET_OPTION = re.compile(r"(?:--unset=|-u).+")


def _env_command_index(tokens: list[str], index: int) -> int | None:
    """Return the command after supported GNU ``env`` options and assignments."""
    command_index = index + 1
    while command_index < len(tokens):
        argument = tokens[command_index]
        if argument == "--":
            return command_index + 1
        option_arity = _env_option_arity(argument)
        if option_arity is not None:
            command_index += option_arity
            continue
        if SHELL_ASSIGNMENT.match(argument) is None:
            return command_index
        command_index += 1
    return None


def _env_option_arity(argument: str) -> int | None:
    """Return how many tokens a supported GNU ``env`` option consumes."""
    if argument in ENV_OPTION_ARITY:
        return ENV_OPTION_ARITY[argument]
    if _is_inline_env_unset_option(argument):
        return 1
    return None


def _is_inline_env_unset_option(argument: str) -> bool:
    """Return whether an unset option carries its name in the same token."""
    return ENV_INLINE_UNSET_OPTION.fullmatch(argument) is not None


def _is_unlocked_project_command(command: list[str], *, is_makefile: bool) -> bool:
    """Return whether one project invocation omits its lock enforcement.

    Returns
    -------
    bool
        Whether the project command's uv option prefix omits ``--locked``.
    """
    tokens = _normalise_make_command(command, is_makefile=is_makefile)
    subcommand_index = _uv_subcommand_index(tokens, is_makefile=is_makefile)
    if subcommand_index is None:
        return False

    subcommand = tokens[subcommand_index]
    arguments = tokens[subcommand_index + 1 :]
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
        for command in _shell_commands(line, line_number=line_number)
        if _is_unlocked_project_command(command, is_makefile=is_makefile)
    ]
