"""Scan project commands for compliance with the committed lock policy.

The helpers parse Makefile recipes and GitHub Actions ``run:`` scripts. They
preserve shell command boundaries, recognize supported Make prefixes and uv
options, and report unlocked project ``uv run`` and ``uv sync`` commands with
source-line context. Unsupported shell syntax raises a focused error instead
of silently skipping a command. The contract tests in
``tests/test_uv_command_contracts.py`` use this scanner to enforce the
repository's committed ``uv.lock`` policy.
"""

from __future__ import annotations

import re
import shlex


class ShellCommandSyntaxError(ValueError):
    """A shell command could not be parsed with source-line context."""


class UnsupportedCommandOptionError(ValueError):
    """An unsupported command option prevents safe project-lock scanning."""


class UnsupportedUvOptionError(UnsupportedCommandOptionError):
    """A global uv option is not covered by the command contract scanner."""


class UnsupportedEnvOptionError(UnsupportedCommandOptionError):
    """An env option prevents safe inspection of its wrapped command."""


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
    "{",
})
ENV_FLAGS = frozenset({
    "-",
    "-0",
    "--block-signal",
    "--default-signal",
    "--debug",
    "--ignore-signal",
    "--ignore-environment",
    "--list-signal-handling",
    "--null",
    "-i",
    "-v",
})
ENV_OPTIONS_WITH_VALUES = frozenset({
    "-a",
    "--argv0",
    "--env0-from",
    "-C",
    "--chdir",
    "-u",
    "--unset",
})
ENV_INLINE_OPTIONS_WITH_VALUES = re.compile(
    r"(?:--(?:argv0|chdir|env0-from|unset)=|-a|-C|-u).+"
)
ENV_OPTIONAL_VALUE_OPTIONS = frozenset({
    "--block-signal",
    "--default-signal",
    "--ignore-signal",
})
MAKE_ASSIGNMENT_OPERATORS = frozenset({"=", "?=", ":=", "+=", "!=", "::="})
UV_INFORMATIONAL_OPTIONS = frozenset({"--help", "--version", "-h", "-V"})
SHELL_HEREDOC_PART = re.compile(
    r"(?P<comment>(?<![^\s;&|()<>])\#[^\n]*)|'[^']*'|"
    r'"(?:\\.|[^"\\])*"|\\.|(?P<operator>(?<!<)(?:<<-(?!<)|<<(?!<)))'
)
SHELL_QUOTED_TEXT = re.compile(r"'[^']*'|\"(?:\\.|[^\"\\])*\"")
SHELL_COMMAND_SUBSTITUTION = re.compile(r"\$\((?!\()")
MAKE_COMMAND_SUBSTITUTION = re.compile(r"\$\$\((?!\()")
QUOTED_OPEN_PARENTHESIS = "\ue000"
QUOTED_CLOSE_PARENTHESIS = "\ue001"
# Keep global option arity aligned with `uv --help`; their values must not be
# mistaken for the command whose arguments no longer belong to uv.
UV_GLOBAL_OPTIONS_WITH_VALUES = frozenset({
    "--allow-insecure-host",
    "--cache-dir",
    "--color",
    "--config-file",
    "--directory",
    "--project",
    "--preview-features",
    "--python-preference",
})
UV_GLOBAL_FLAGS = frozenset({
    "--isolated",
    "--managed-python",
    "--native-tls",
    "--no-cache",
    "--no-cache-dir",
    "--no-config",
    "--no-managed-python",
    "--no-progress",
    "--no-python-downloads",
    "--offline",
    "--preview",
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
    "--preview-features",
    "--python-preference",
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

    Notes
    -----
    An unterminated heredoc raises ``ShellCommandSyntaxError`` before command
    analysis returns any results.
    """
    logical_lines: list[tuple[int, str]] = []
    fragments: list[str] = []
    pending_heredocs: list[tuple[str, bool, int]] = []
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
    _raise_for_unterminated_heredocs(pending_heredocs)
    return logical_lines


def _raise_for_unterminated_heredocs(
    pending_heredocs: list[tuple[str, bool, int]],
) -> None:
    """Fail with the source line when heredoc input reaches end of file.

    Raises
    ------
    ShellCommandSyntaxError
        A heredoc is missing its closing delimiter at end of input.
    """
    if not pending_heredocs:
        return
    delimiters = ", ".join(repr(delimiter) for delimiter, _, _ in pending_heredocs)
    message = (
        f"Unterminated heredoc from shell line {pending_heredocs[0][2]}: "
        f"no closing delimiter for {delimiters}"
    )
    raise ShellCommandSyntaxError(message)


def _skip_shell_line(
    line: str,
    fragments: list[str],
    pending_heredocs: list[tuple[str, bool, int]],
) -> bool:
    """Return whether the physical line is a comment or heredoc body."""
    if _consume_heredoc_line(line, pending_heredocs):
        return True
    return not fragments and line.lstrip().startswith("#")


def _append_shell_fragment(line: str, fragments: list[str]) -> bool:
    """Append a physical line and report whether its shell continuation remains open."""
    stripped = line.rstrip()
    trailing_backslashes = len(stripped) - len(stripped.rstrip("\\"))
    continues = trailing_backslashes % 2 == 1
    fragments.append(stripped[:-1].strip() if continues else line.strip())
    return continues


def _finish_logical_line(
    fragments: list[str],
    start_line: int,
    logical_lines: list[tuple[int, str]],
    pending_heredocs: list[tuple[str, bool, int]],
) -> None:
    """Store a complete shell line or retain it while a quote remains open."""
    logical_line = " ".join(fragments)
    if _has_incomplete_shell_quote(logical_line):
        return
    logical_lines.append((start_line, logical_line))
    pending_heredocs.extend(
        (delimiter, strip_tabs, start_line)
        for delimiter, strip_tabs in _heredoc_delimiters(logical_line)
    )
    fragments.clear()


def _shell_tokens(line: str, *, punctuation_chars: str) -> list[str]:
    """Tokenize shell input with the punctuation used by the caller."""
    lexer = shlex.shlex(
        _strip_shell_comment(line), posix=True, punctuation_chars=punctuation_chars
    )
    lexer.whitespace_split = True
    lexer.commenters = ""
    return list(lexer)


def _strip_shell_comment(line: str) -> str:
    """Drop a comment beginning at an unquoted shell word boundary."""
    for match in SHELL_HEREDOC_PART.finditer(line):
        if match.group("comment") is not None:
            return line[: match.start()]
    return line


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
    arithmetic_depth = 0
    position = 0
    for match in SHELL_HEREDOC_PART.finditer(line):
        arithmetic_depth = _advance_arithmetic_expansion_depth(
            line[position : match.start()], arithmetic_depth
        )
        position = match.end()
        if match.group("comment") is not None:
            break
        operator = match.group("operator")
        if operator is None or arithmetic_depth:
            continue
        delimiter = _first_shell_word(line[match.end() :])
        if delimiter is not None:
            delimiters.append((delimiter, operator.endswith("-")))
    return delimiters


def _advance_arithmetic_expansion_depth(source: str, depth: int) -> int:
    """Track unquoted arithmetic expansions before a possible heredoc token."""
    index = 0
    while index < len(source):
        if source.startswith("$((", index):
            depth += 1
            index += 3
        elif depth and source.startswith("))", index):
            depth -= 1
            index += 2
        else:
            index += 1
    return depth


def _first_shell_word(source: str) -> str | None:
    """Return the first shell word from the text following an operator."""
    lexer = shlex.shlex(source, posix=True, punctuation_chars=";&|<>")
    lexer.whitespace_split = True
    word = lexer.get_token()
    return word if isinstance(word, str) else None


def _consume_heredoc_line(
    line: str, pending_heredocs: list[tuple[str, bool, int]]
) -> bool:
    """Consume heredoc body text, dropping a delimiter when it closes."""
    if not pending_heredocs:
        return False
    delimiter, strip_tabs, _ = pending_heredocs[0]
    candidate = line.lstrip("\t") if strip_tabs else line
    if candidate == delimiter:
        pending_heredocs.pop(0)
    return True


def run_step_scripts(document: object) -> list[str]:
    """Collect ``run:`` script values from a parsed workflow document.

    Parameters
    ----------
    document : object
        Parsed workflow or composite-action data.

    Returns
    -------
    list of str
        String ``run`` values found at any depth, in document order.
    """
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


def _shell_commands(
    line: str, *, line_number: int, is_makefile: bool
) -> list[list[str]]:
    """Split a logical shell line at command separators.

    Returns
    -------
    list of list of str
        Shell command token groups, with separators removed.
    """
    tokens = _shell_command_tokens(
        line, line_number=line_number, is_makefile=is_makefile
    )
    commands = _commands_from_tokens(tokens, is_makefile=is_makefile)
    return [
        [
            token.replace(QUOTED_OPEN_PARENTHESIS, "(").replace(
                QUOTED_CLOSE_PARENTHESIS, ")"
            )
            for token in command
        ]
        for command in commands
    ]


def _shell_command_tokens(
    line: str, *, line_number: int, is_makefile: bool
) -> list[str]:
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
    token_source = _protect_quoted_parentheses(
        line, line_number=line_number, is_makefile=is_makefile
    )
    punctuation_chars = ";&|" if is_makefile else ";&|()"
    try:
        return _shell_tokens(token_source, punctuation_chars=punctuation_chars)
    except ValueError as error:
        message = f"Unbalanced quote at shell line {line_number}: {line!r}"
        raise ShellCommandSyntaxError(message) from error


def _protect_quoted_parentheses(
    line: str, *, line_number: int, is_makefile: bool
) -> str:
    """Keep literal quoted parentheses out of shell command boundaries."""

    def replace(match: re.Match[str]) -> str:
        quoted = match.group(0)
        substitution = (
            MAKE_COMMAND_SUBSTITUTION if is_makefile else SHELL_COMMAND_SUBSTITUTION
        )
        if quoted.startswith('"') and substitution.search(quoted):
            message = (
                f"Unsupported quoted command substitution at shell line "
                f"{line_number}: {line!r}"
            )
            raise ShellCommandSyntaxError(message)
        return quoted.replace("(", QUOTED_OPEN_PARENTHESIS).replace(
            ")", QUOTED_CLOSE_PARENTHESIS
        )

    return SHELL_QUOTED_TEXT.sub(replace, line)


def _commands_from_tokens(tokens: list[str], *, is_makefile: bool) -> list[list[str]]:
    """Group shell tokens by command separators, omitting the separators."""
    commands: list[list[str]] = []
    current: list[str] = []
    separators = ";&|" if is_makefile else ";&|()"

    for token in tokens:
        if token and all(character in separators for character in token):
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
    if not is_makefile:
        return [tokens[0].removeprefix("@"), *tokens[1:]] if tokens else tokens

    _strip_make_recipe_prefixes(tokens)
    try:
        name, operator, *recipe = tokens
    except ValueError:
        return tokens
    if MAKE_VARIABLE.fullmatch(name) and operator in MAKE_ASSIGNMENT_OPERATORS:
        return recipe
    return tokens


def _strip_make_recipe_prefixes(tokens: list[str]) -> None:
    """Remove Make's silent, ignore-error and recursive recipe prefixes."""
    while tokens and all(character in "@-+" for character in tokens[0]):
        tokens.pop(0)
    if tokens:
        tokens[0] = tokens[0].lstrip("@-+")


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
    command_index = _uv_global_option_end(tokens, index)
    if command_index is None:
        return None
    if command_index >= len(tokens):
        return None
    if tokens[command_index] not in {"run", "sync"}:
        return None
    return command_index


def _uv_global_option_end(tokens: list[str], index: int) -> int | None:
    """Return the command index after global options, if they permit execution."""
    while index < len(tokens):
        option = tokens[index]
        if option in UV_INFORMATIONAL_OPTIONS:
            return None
        if option == "--":
            return index + 1
        if not option.startswith("-"):
            return index
        index += _known_uv_global_option_arity(option)
    return index


def _known_uv_global_option_arity(option: str) -> int:
    """Return the known option arity or require an explicit scanner update."""
    arity = _uv_global_option_arity(option)
    if arity is None:
        message = (
            f"Unrecognized uv global option {option!r}; add it to "
            "UV_GLOBAL_FLAGS or UV_GLOBAL_OPTIONS_WITH_VALUES"
        )
        raise UnsupportedUvOptionError(message)
    return arity


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
        if argument.startswith("-"):
            message = (
                f"Unsupported GNU env option {argument!r}; add it to "
                "ENV_FLAGS or ENV_OPTIONS_WITH_VALUES"
            )
            raise UnsupportedEnvOptionError(message)
        if SHELL_ASSIGNMENT.match(argument) is None:
            return command_index
        command_index += 1
    return None


def _env_option_arity(argument: str) -> int | None:
    """Return how many tokens a supported GNU ``env`` option consumes."""
    if argument in ENV_OPTION_ARITY:
        return ENV_OPTION_ARITY[argument]
    if _is_inline_env_option_with_value(argument):
        return 1
    if any(argument.startswith(f"{option}=") for option in ENV_OPTIONAL_VALUE_OPTIONS):
        return 1
    return None


def _is_inline_env_option_with_value(argument: str) -> bool:
    """Return whether a value-taking env option carries its value inline."""
    return ENV_INLINE_OPTIONS_WITH_VALUES.fullmatch(argument) is not None


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


def unlocked_project_commands(
    text: str, *, is_makefile: bool = False
) -> list[tuple[int, str]]:
    """Return project commands that use ``uv run`` or ``uv sync`` unlocked.

    Parameters
    ----------
    text : str
        Makefile recipe or shell script source.
    is_makefile : bool, default=False
        Whether Make recipe prefixes and variable assignments are present.

    Returns
    -------
    list of tuple of int and str
        One-based logical line numbers paired with each offending command.

    Notes
    -----
    Parsing errors propagate as ``ShellCommandSyntaxError``. Unsupported
    command options propagate as ``UnsupportedCommandOptionError`` with the
    specific uv or env option type retained.
    """
    violations: list[tuple[int, str]] = []
    for line_number, line in _logical_lines(text):
        violations.extend(
            _unlocked_commands_on_line(
                line, line_number=line_number, is_makefile=is_makefile
            )
        )
    return violations


def _unlocked_commands_on_line(
    line: str, *, line_number: int, is_makefile: bool
) -> list[tuple[int, str]]:
    """Find unlocked invocations in one logical line with useful errors."""
    violations: list[tuple[int, str]] = []
    for command in _shell_commands(
        line, line_number=line_number, is_makefile=is_makefile
    ):
        command_text = " ".join(command)
        try:
            is_unlocked = _is_unlocked_project_command(command, is_makefile=is_makefile)
        except UnsupportedCommandOptionError as error:
            message = (
                f"Unsupported command option at line {line_number}: "
                f"{command_text!r}: {error}"
            )
            raise type(error)(message) from error
        if is_unlocked:
            violations.append((line_number, command_text))
    return violations
