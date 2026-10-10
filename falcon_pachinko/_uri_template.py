"""Compile URI templates into regular expressions safely."""

from __future__ import annotations

import re

_MAX_PARAMETERS_PER_SEGMENT = 2


def _parse_parameter(template: str, start: int) -> tuple[str, int]:
    """Return a validated parameter name and the first index after its brace.

    Returns
    -------
    tuple[str, int]
        The parameter name and the first index after its closing brace.

    Raises
    ------
    ValueError
        If braces are malformed or the parameter name is invalid.
    """
    closing_brace = template.find("}", start + 1)
    nested_brace = template.find(
        "{", start + 1, closing_brace if closing_brace >= 0 else len(template)
    )
    if nested_brace >= 0:
        placeholder_end = closing_brace + 1 if closing_brace >= 0 else len(template)
        placeholder = template[start:placeholder_end]
        msg = (
            f"Nested or doubled brace in parameter {placeholder!r} "
            f"in template: {template}"
        )
        raise ValueError(msg)
    if closing_brace < 0:
        parameter_name = template[start + 1 :]
        msg = (
            f"Unmatched opening brace for parameter {parameter_name!r} "
            f"in template: {template}"
        )
        raise ValueError(msg)

    parameter_name = template[start + 1 : closing_brace]
    if not parameter_name:
        msg = f"Empty parameter name in template: {template}"
        raise ValueError(msg)
    if not parameter_name.isidentifier():
        msg = f"Invalid parameter name {parameter_name!r} in template: {template}"
        raise ValueError(msg)

    return parameter_name, closing_brace + 1


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
        if char != "{":
            index += 1
            continue

        if literal_start < index:
            tokens.append((False, template[literal_start:index]))

        parameter_name, index = _parse_parameter(template, index)
        if parameter_name in parameter_names:
            msg = f"Duplicate parameter name {parameter_name!r} in template: {template}"
            raise ValueError(msg)

        parameter_names.add(parameter_name)
        tokens.append((True, parameter_name))
        literal_start = index

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
            continue

        parts = text.split("/")
        for index, part in enumerate(parts):
            if part:
                segments[-1].append((False, part))
            if index < len(parts) - 1:
                segments.append([])
    return segments


def _is_terminal_parameter_pair(
    segment: list[tuple[bool, str]], parameter_positions: list[int]
) -> bool:
    """Return whether exactly two parameters are adjacent at segment end."""
    if len(parameter_positions) != _MAX_PARAMETERS_PER_SEGMENT:
        return False
    first, second = parameter_positions
    return second == first + 1 and second == len(segment) - 1


def _compile_template_with_suffix(template: str, suffix: str) -> re.Pattern[str]:
    """Compile a template with escaped literals and ``suffix`` appended.

    Returns
    -------
    re.Pattern[str]
        The compiled regular expression.

    Raises
    ------
    ValueError
        If the template contains malformed braces or parameter names.
    """
    stripped_template = template.rstrip("/")
    try:
        tokens = _tokenize_template(stripped_template)
    except ValueError as exc:
        if stripped_template == template:
            raise
        msg = f"{exc} (original template: {template})"
        raise ValueError(msg) from exc
    pattern_parts = [
        f"(?P<{text}>[^/]+)" if is_parameter else re.escape(text)
        for is_parameter, text in tokens
    ]
    return re.compile(f"^{''.join(pattern_parts)}{suffix}")


def compile_uri_template(template: str) -> re.Pattern[str]:
    """Compile a URI template for full-path matching.

    Literal text is matched exactly. Named parameters use Python identifier
    names and capture one or more characters up to the next slash. At most two
    parameters may appear consecutively; this bounds backtracking for
    ambiguous captures.

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
