"""Compile URI templates into safe regular expressions.

The compiler escapes literal route text and turns named placeholders into
non-slash path-segment captures. Use :func:`compile_uri_template` for full-path
matching::

    match = compile_uri_template("/rooms/{room}").fullmatch("/rooms/a")
"""

from __future__ import annotations

import re

_MAX_PARAMETERS_PER_SEGMENT = 2


def _raise_nested_brace(template: str, start: int, closing_brace: int) -> None:
    """Raise the template error for a nested or doubled opening brace."""
    placeholder_end = closing_brace + 1 if closing_brace >= 0 else len(template)
    placeholder = template[start:placeholder_end]
    msg = (
        f"Nested or doubled brace in parameter {placeholder!r} in template: {template}"
    )
    raise ValueError(msg)


def _find_parameter_closing_brace(template: str, start: int) -> int:
    """Return the closing brace index after checking for malformed braces."""
    closing_brace = template.find("}", start + 1)
    nested_brace = template.find(
        "{", start + 1, closing_brace if closing_brace >= 0 else len(template)
    )
    if nested_brace >= 0:
        _raise_nested_brace(template, start, closing_brace)
    if closing_brace < 0:
        parameter_name = template[start + 1 :]
        msg = (
            f"Unmatched opening brace for parameter {parameter_name!r} "
            f"in template: {template}"
        )
        raise ValueError(msg)
    return closing_brace


def _validate_parameter_name(parameter_name: str, template: str) -> None:
    """Raise when ``parameter_name`` is empty or not a Python identifier."""
    if not parameter_name:
        msg = f"Empty parameter name in template: {template}"
        raise ValueError(msg)
    if not parameter_name.isidentifier():
        msg = f"Invalid parameter name {parameter_name!r} in template: {template}"
        raise ValueError(msg)


def _parse_parameter(template: str, start: int) -> tuple[str, int]:
    """Return a validated parameter name and the first index after its brace.

    Returns
    -------
    tuple[str, int]
        The parameter name and the first index after its closing brace.
    """
    closing_brace = _find_parameter_closing_brace(template, start)
    parameter_name = template[start + 1 : closing_brace]
    _validate_parameter_name(parameter_name, template)
    return parameter_name, closing_brace + 1


def _append_literal_token(
    tokens: list[tuple[bool, str]], template: str, start: int, end: int
) -> None:
    """Append non-empty literal text before a placeholder."""
    if start < end:
        tokens.append((False, template[start:end]))


def _append_parameter_token(
    tokens: list[tuple[bool, str]],
    parameter_name: str,
    template: str,
    parameter_names: set[str],
) -> None:
    """Append a unique validated placeholder."""
    if parameter_name in parameter_names:
        msg = f"Duplicate parameter name {parameter_name!r} in template: {template}"
        raise ValueError(msg)

    parameter_names.add(parameter_name)
    tokens.append((True, parameter_name))


def _tokenize_template(template: str) -> list[tuple[bool, str]]:
    """Return ordered literal and parameter tokens from ``template``.

    Each token is ``(is_parameter, text)``. Braces are reserved for parameter
    names, so malformed or ambiguous brace pairs fail before regex compilation.

    Returns
    -------
    list[tuple[bool, str]]
        Ordered tokens, with ``True`` marking placeholder names.

    Raises
    ------
    ValueError
        If braces or names are invalid, or a path segment has an ambiguous
        multiple-parameter form.
    """
    tokens: list[tuple[bool, str]] = []
    parameter_names: set[str] = set()
    index = 0
    literal_start = 0

    while index < len(template):
        char = template[index]
        if char == "}":
            msg = f"Unmatched closing brace '}}' in template: {template}"
            raise ValueError(msg)
        if char == "{":
            _append_literal_token(tokens, template, literal_start, index)
            parameter_name, index = _parse_parameter(template, index)
            _append_parameter_token(tokens, parameter_name, template, parameter_names)
            literal_start = index
        else:
            index += 1

    if literal_start < len(template):
        tokens.append((False, template[literal_start:]))

    _validate_parameter_segments(tokens, template)
    return tokens


def _validate_parameter_segments(tokens: list[tuple[bool, str]], template: str) -> None:
    """Reject ambiguous parameter layouts within one path segment."""
    for segment in _split_template_segments(tokens):
        parameter_positions = [
            index for index, (is_parameter, _) in enumerate(segment) if is_parameter
        ]
        if len(parameter_positions) <= 1 or _is_terminal_parameter_pair(
            segment, parameter_positions
        ):
            continue

        parameter_names = [segment[index][1] for index in parameter_positions]
        msg = (
            f"Ambiguous parameters {parameter_names!r} in one path segment "
            f"in template: {template}"
        )
        raise ValueError(msg)


def _split_template_segments(
    tokens: list[tuple[bool, str]],
) -> list[list[tuple[bool, str]]]:
    """Split template tokens at literal slashes while preserving token order."""
    segments: list[list[tuple[bool, str]]] = [[]]
    for is_parameter, text in tokens:
        if is_parameter:
            segments[-1].append((True, text))
        else:
            _append_literal_segments(segments, text)
    return segments


def _append_literal_segments(segments: list[list[tuple[bool, str]]], text: str) -> None:
    """Append literal text to the current segment, splitting on slashes."""
    parts = text.split("/")
    for part in parts[:-1]:
        if part:
            segments[-1].append((False, part))
        segments.append([])
    if parts[-1]:
        segments[-1].append((False, parts[-1]))


def _is_terminal_parameter_pair(
    segment: list[tuple[bool, str]], parameter_positions: list[int]
) -> bool:
    """Return whether exactly two parameters are adjacent at segment end."""
    if len(parameter_positions) != _MAX_PARAMETERS_PER_SEGMENT:
        return False
    first, second = parameter_positions
    return second == first + 1 and second == len(segment) - 1


def _render_template_tokens(tokens: list[tuple[bool, str]]) -> str:
    """Render validated tokens as escaped literals and named captures."""
    pattern_parts = []
    for index, (is_parameter, text) in enumerate(tokens):
        if is_parameter:
            # The greedy first capture leaves one character for its adjacent
            # partner, avoiding quadratic retries on later segment mismatches.
            is_adjacent_capture = index > 0 and tokens[index - 1][0]
            quantifier = "" if is_adjacent_capture else "+"
            pattern_parts.append(f"(?P<{text}>[^/]{quantifier})")
        else:
            pattern_parts.append(re.escape(text))
    return "".join(pattern_parts)


def _compile_template_with_suffix(template: str, suffix: str) -> re.Pattern[str]:
    """Compile a template with escaped literals and ``suffix`` appended.

    Returns
    -------
    re.Pattern[str]
        The compiled regular expression.

    Raises
    ------
    ValueError
        If braces are malformed, a parameter name is invalid or duplicated,
        or a path segment has an ambiguous multiple-parameter layout.
    """
    stripped_template = template.rstrip("/")
    try:
        tokens = _tokenize_template(stripped_template)
    except ValueError as exc:
        if stripped_template == template:
            raise
        msg = f"{exc} (original template: {template})"
        raise ValueError(msg) from exc
    return re.compile(f"^{_render_template_tokens(tokens)}{suffix}")


def compile_uri_template(template: str) -> re.Pattern[str]:
    """Compile a URI template for full-path matching.

    Literal text is matched exactly. Named parameters use Python identifier
    names and capture one or more non-slash characters. A path segment may
    contain one parameter or a terminal adjacent pair.

    Parameters
    ----------
    template : str
        URI template containing literal text and named placeholders.

    Returns
    -------
    re.Pattern[str]
        The compiled full-path regular expression.
    """
    return _compile_template_with_suffix(template, "/?$")


def _compile_prefix_template(template: str) -> re.Pattern[str]:
    """Compile ``template`` to match a path prefix."""
    return _compile_template_with_suffix(template, "(?:/|$)")
