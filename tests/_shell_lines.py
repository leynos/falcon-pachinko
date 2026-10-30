"""Parse shell source for repository command-contract checks.

The helpers join physical lines, skip heredoc data, preserve quoted arguments,
and split command groups with source locations. The uv command scanner uses
these boundaries to inspect project invocations without mistaking shell data
for commands or silently accepting syntax that can hide a uv invocation.
"""

from __future__ import annotations

import re
import shlex


class ShellCommandSyntaxError(ValueError):
    """A shell command could not be parsed with source-line context."""


SHELL_HEREDOC_PART = re.compile(
    r"(?P<comment>(?<![^\s;&|()<>])\#[^\n]*)|'[^']*'|"
    r'"(?:\\.|[^"\\])*"|\\.|(?P<operator>(?<!<)(?:<<-(?!<)|<<(?!<)))'
)


SHELL_QUOTED_TEXT = re.compile(r"'[^']*'|\"(?:\\.|[^\"\\])*\"")


SHELL_COMMAND_SUBSTITUTION = re.compile(r"\$\((?!\()")


SHELL_BACKTICK_SUBSTITUTION = re.compile(r"`(?:\\.|[^`\\])*`")


MAKE_COMMAND_SUBSTITUTION = re.compile(r"\$\$\((?!\()")


UV_COMMAND_IN_SUBSTITUTION = re.compile(r"\buv\b|\$\$?\(?UV\)?|\$\{UV\}")


QUOTED_OPEN_PARENTHESIS = "\ue000"


QUOTED_CLOSE_PARENTHESIS = "\ue001"


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
    contains_comment = _strip_shell_comment(line) != line
    continues = trailing_backslashes % 2 == 1 and not contains_comment
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


def _quoted_substitution_contains_uv_command(
    quoted: str, substitution: re.Pattern[str]
) -> bool:
    """Return whether a quoted shell substitution contains a uv command."""
    if not quoted.startswith('"'):
        return False
    if not substitution.search(quoted):
        return False
    return bool(UV_COMMAND_IN_SUBSTITUTION.search(quoted))


def _backtick_substitution_contains_uv_command(line: str) -> bool:
    """Return whether active backticks contain a command that may invoke uv."""
    source = _strip_shell_comment(line)
    single_quoted_spans = [
        match.span()
        for match in SHELL_QUOTED_TEXT.finditer(source)
        if match.group().startswith("'")
    ]
    return any(
        _backtick_match_contains_uv_command(source, match, single_quoted_spans)
        for match in SHELL_BACKTICK_SUBSTITUTION.finditer(source)
    )


def _backtick_match_contains_uv_command(
    source: str,
    match: re.Match[str],
    single_quoted_spans: list[tuple[int, int]],
) -> bool:
    """Return whether one active backtick substitution may invoke uv."""
    prefix = source[: match.start()]
    preceding_backslashes = len(prefix) - len(prefix.rstrip("\\"))
    if preceding_backslashes % 2:
        return False
    if any(
        start <= match.start() and match.end() <= end
        for start, end in single_quoted_spans
    ):
        return False
    return bool(UV_COMMAND_IN_SUBSTITUTION.search(match.group()[1:-1]))


def _protect_quoted_parentheses(
    line: str, *, line_number: int, is_makefile: bool
) -> str:
    """Protect quoted parentheses and reject substitutions that can hide uv."""
    if _backtick_substitution_contains_uv_command(line):
        message = (
            f"Unsupported backtick command substitution at shell line "
            f"{line_number}: {line!r}"
        )
        raise ShellCommandSyntaxError(message)

    def replace(match: re.Match[str]) -> str:
        quoted = match.group(0)
        substitution = (
            MAKE_COMMAND_SUBSTITUTION if is_makefile else SHELL_COMMAND_SUBSTITUTION
        )
        if _quoted_substitution_contains_uv_command(quoted, substitution):
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
