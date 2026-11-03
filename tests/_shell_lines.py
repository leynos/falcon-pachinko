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


SHELL_QUOTED_TEXT = re.compile(r"\\.|'[^']*'|\"(?:\\.|[^\"\\])*\"")


SHELL_COMMAND_SUBSTITUTION = re.compile(r"\$\((?!\()")


SHELL_BACKTICK_SUBSTITUTION = re.compile(r"\\.|(?P<substitution>`(?:\\.|[^`\\])*`)")


MAKE_COMMAND_SUBSTITUTION = re.compile(r"\$\$?\((?!\()")


SINGLE_QUOTED_TEXT = re.compile(r"'[^']*'")


MAKEFILE_PROJECT_UV_COMMAND = re.compile(
    r"(?:\buv\b|\$\$?\(UV\)|\$\{UV\})"
    r"(?:(?:\s+-[^\s()]+)(?:\s+[^\s()]+)?)*\s+(?:run|sync)\b"
)


UV_COMMAND_IN_SUBSTITUTION = re.compile(r"\buv\b|\$\$?\(?UV\)?|\$\{UV\}")


QUOTED_OPEN_PARENTHESIS = "\ue000"


QUOTED_CLOSE_PARENTHESIS = "\ue001"


def logical_lines(text: str) -> list[tuple[int, str]]:
    """Return source lines joined across shell or Make continuations.

    Parameters
    ----------
    text : str
        Makefile recipe or shell script source.

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
    continues_from_previous = False

    for line_number, line in enumerate(text.splitlines(), start=1):
        if _skip_shell_line(line, fragments, pending_heredocs):
            continue
        if not fragments:
            start_line = line_number
        elif not continues_from_previous:
            fragments.append(" ")
        continues_from_previous = _append_shell_fragment(
            line, fragments, previously_continued=continues_from_previous
        )
        if not continues_from_previous:
            _finish_logical_line(fragments, start_line, logical_lines, pending_heredocs)

    if fragments:
        logical_lines.append((start_line, "".join(fragments)))
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


def _append_shell_fragment(
    line: str, fragments: list[str], *, previously_continued: bool = False
) -> bool:
    """Append a physical line and report whether its shell continuation remains open."""
    stripped = line.rstrip()
    trailing_backslashes = len(stripped) - len(stripped.rstrip("\\"))
    contains_comment = _strip_shell_comment(line) != line
    continues = trailing_backslashes % 2 == 1 and not contains_comment
    fragment = stripped[:-1] if continues else line
    if not previously_continued and not continues:
        fragment = fragment.strip()
    fragments.append(fragment)
    return continues


def _finish_logical_line(
    fragments: list[str],
    start_line: int,
    logical_lines: list[tuple[int, str]],
    pending_heredocs: list[tuple[str, bool, int]],
) -> None:
    """Store a complete shell line or retain it while a quote remains open."""
    logical_line = "".join(fragments)
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


def shell_commands(
    line: str, *, line_number: int, is_makefile: bool
) -> list[list[str]]:
    """Split a logical shell line at command separators.

    Parameters
    ----------
    line : str
        One logical shell line.
    line_number : int
        One-based starting line number, used in error messages.
    is_makefile : bool
        Whether Make recipe syntax applies to the line.

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


def _command_substitution_contains_uv_command(
    line: str, substitution: re.Pattern[str]
) -> bool:
    """Return whether an active command substitution can invoke uv."""
    source = _strip_shell_comment(line)
    index = 0
    quote: str | None = None
    while index < len(source):
        candidate = _quoted_substitution_body(source, index, quote, substitution)
        if candidate is not None:
            end, body = candidate
            if UV_COMMAND_IN_SUBSTITUTION.search(body):
                return True
            index = end + 1
            continue
        quote, index = _advance_shell_quote(source, index, quote)
    return False


def _quoted_substitution_body(
    source: str,
    index: int,
    quote: str | None,
    substitution: re.Pattern[str],
) -> tuple[int, str] | None:
    """Return a double-quoted substitution body, including unfinished input."""
    if quote != '"':
        return None
    opening = substitution.match(source, index)
    if opening is None:
        return None
    end = _command_substitution_end(source, opening.end(), substitution)
    if end is None:
        # Incomplete syntax must not conceal a possible project command.
        end = len(source)
    body = SINGLE_QUOTED_TEXT.sub(" ", source[opening.end() : end])
    return end, body


class _ParameterExpansionBoundaries:
    """Keep parameter words separate from their enclosing command groups."""

    def __init__(self) -> None:
        self._contexts: list[tuple[int, str | None]] = []

    def advance(
        self, source: str, index: int, quote: str | None, depth: int
    ) -> tuple[str | None, int] | None:
        """Advance within a parameter word at the current command depth."""
        if quote != "'" and source.startswith("${", index):
            self._contexts.append((depth, quote))
            return quote, index + 2
        if not self._contexts or self._contexts[-1][0] != depth:
            return None
        if source[index] == "}" and quote == self._contexts[-1][1]:
            self._contexts.pop()
        return _advance_shell_quote(source, index, quote)


def _command_substitution_end(
    source: str, start: int, substitution: re.Pattern[str]
) -> int | None:
    """Find the matching close for a substitution, including nested forms."""
    quote: str | None = None
    enclosing_quotes: list[str | None] = []
    parameter_expansions = _ParameterExpansionBoundaries()
    index = start
    while index < len(source):
        opening = _nested_substitution_at(source, index, quote, substitution)
        if opening is not None:
            enclosing_quotes.append(quote)
            quote = None
            index = opening.end()
            continue
        parameter_step = parameter_expansions.advance(
            source, index, quote, len(enclosing_quotes) + 1
        )
        if parameter_step is not None:
            quote, index = parameter_step
            continue
        quote, index, is_closed = _advance_substitution_body(
            source, index, quote, enclosing_quotes
        )
        if is_closed:
            return index
    return None


def _advance_substitution_body(
    source: str, index: int, quote: str | None, enclosing_quotes: list[str | None]
) -> tuple[str | None, int, bool]:
    """Advance shell data or groups, reporting the outer substitution close."""
    character = source[index]
    if character == "(" and quote is None:
        enclosing_quotes.append(None)
        return quote, index + 1, False
    if character != ")" or quote is not None:
        quote, index = _advance_shell_quote(source, index, quote)
        return quote, index, False
    if not enclosing_quotes:
        return quote, index, True
    return enclosing_quotes.pop(), index + 1, False


def _nested_substitution_at(
    source: str,
    index: int,
    quote: str | None,
    substitution: re.Pattern[str],
) -> re.Match[str] | None:
    """Return a nested active substitution, excluding single-quoted data."""
    if quote == "'":
        return None
    return substitution.match(source, index)


def _advance_shell_quote(
    source: str, index: int, quote: str | None
) -> tuple[str | None, int]:
    """Advance one character while respecting shell quotes and escapes."""
    character = source[index]
    if character == "\\" and quote != "'":
        return quote, index + 2
    match quote:
        case "'":
            return (None if character == "'" else quote), index + 1
        case '"':
            return (None if character == '"' else quote), index + 1
        case _:
            match character:
                case "'":
                    return "'", index + 1
                case '"':
                    return '"', index + 1
                case _:
                    return None, index + 1


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
        if match.group("substitution") is not None
    )


def _backtick_match_contains_uv_command(
    source: str,
    match: re.Match[str],
    single_quoted_spans: list[tuple[int, int]],
) -> bool:
    """Return whether one active backtick substitution may invoke uv."""
    if any(
        start <= match.start() and match.end() <= end
        for start, end in single_quoted_spans
    ):
        return False
    substitution = match.group("substitution")
    if substitution is None:
        return False
    return bool(UV_COMMAND_IN_SUBSTITUTION.search(substitution[1:-1]))


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
    if is_makefile and _makefile_group_contains_uv_project_command(line):
        message = (
            f"Unsupported Makefile grouping or substitution at shell line "
            f"{line_number}: {line!r}"
        )
        raise ShellCommandSyntaxError(message)
    substitution = (
        MAKE_COMMAND_SUBSTITUTION if is_makefile else SHELL_COMMAND_SUBSTITUTION
    )
    if _command_substitution_contains_uv_command(line, substitution):
        message = (
            f"Unsupported quoted command substitution at shell line {line_number}: "
            f"{line!r}"
        )
        raise ShellCommandSyntaxError(message)

    def replace(match: re.Match[str]) -> str:
        quoted = match.group(0)
        return quoted.replace("(", QUOTED_OPEN_PARENTHESIS).replace(
            ")", QUOTED_CLOSE_PARENTHESIS
        )

    return SHELL_QUOTED_TEXT.sub(replace, line)


def _makefile_group_contains_uv_project_command(line: str) -> bool:
    """Return whether an unquoted Makefile group can hide a project uv call."""
    source = SHELL_QUOTED_TEXT.sub(" ", _strip_shell_comment(line))
    return any(
        MAKEFILE_PROJECT_UV_COMMAND.search(source[start:end])
        for start, end in _makefile_parenthesized_spans(source)
    )


def _makefile_parenthesized_spans(source: str) -> list[tuple[int, int]]:
    """Return spans inside unquoted parentheses, including unfinished groups."""
    open_parentheses: list[int] = []
    spans: list[tuple[int, int]] = []
    for index, character in enumerate(source):
        match character:
            case "(":
                open_parentheses.append(index + 1)
            case ")":
                _append_makefile_group_span(open_parentheses, spans, index)
    spans.extend((start, len(source)) for start in open_parentheses)
    return spans


def _append_makefile_group_span(
    open_parentheses: list[int], spans: list[tuple[int, int]], end: int
) -> None:
    """Close one pending group, leaving unmatched closing brackets untouched."""
    if open_parentheses:
        spans.append((open_parentheses.pop(), end))


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
