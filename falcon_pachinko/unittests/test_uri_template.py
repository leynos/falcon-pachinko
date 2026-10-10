"""Tests for URI-template compilation."""

from __future__ import annotations

import re
import typing as typ

import pytest

from falcon_pachinko.router import (
    _compile_prefix_template,
    compile_uri_template,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc


@pytest.mark.parametrize(
    ("template", "regex_near_miss"),
    [
        ("/child.v1", "/child/v1"),
        ("/a+b", "/aaab"),
        ("/a?b", "/b"),
        ("/a*b", "/aaab"),
        ("/child[.v1]", "/childv1"),
        ("/child]", "/child"),
        ("/a(b)", "/ab"),
        ("/prefix^child", "/prefixchild"),
        ("/child$tail", "/child"),
        ("/a|b", "/a"),
    ],
    ids=[
        "dot",
        "plus",
        "question-mark",
        "asterisk",
        "open-and-close-brackets",
        "close-bracket",
        "parentheses",
        "caret",
        "dollar",
        "alternation",
    ],
)
def test_regex_metacharacters_are_literal(template: str, regex_near_miss: str) -> None:
    """Both matchers accept literal template text and reject regex expansion."""
    full_pattern = compile_uri_template(template)
    prefix_pattern = _compile_prefix_template(template)

    assert full_pattern.fullmatch(template), "full routes should match the literal"
    assert full_pattern.fullmatch(f"{template}/"), (
        "full routes should keep accepting one trailing slash"
    )
    assert not full_pattern.fullmatch(regex_near_miss), (
        "full routes must reject a path accepted only by regex expansion"
    )

    literal_prefix_match = prefix_pattern.match(f"{template}/next")
    assert literal_prefix_match is not None, "prefix routes should match literal text"
    assert literal_prefix_match.group(0).endswith("/"), (
        "prefix matching should consume the separator before the remainder"
    )
    assert prefix_pattern.match(regex_near_miss) is None, (
        "prefix routes must reject a path accepted only by regex expansion"
    )


def test_root_patterns_preserve_existing_slash_semantics() -> None:
    """Root full and prefix patterns retain their established forms."""
    assert compile_uri_template("/").pattern == r"^/?$", (
        "root full matching should retain its optional trailing slash"
    )
    assert _compile_prefix_template("/").pattern == r"^(?:/|$)", (
        "root prefix matching should accept the empty and slash paths"
    )


@pytest.mark.parametrize("compiler", [compile_uri_template, _compile_prefix_template])
@pytest.mark.parametrize(
    "template",
    [
        "{}",
        "{a",
        "a}",
        "{a{b}}",
        "{{id}}",
        "{1a}",
        "{a-b}",
        "{id:int}",
        "/{id}/{id}",
    ],
    ids=[
        "empty-name",
        "unmatched-opening-brace",
        "unmatched-closing-brace",
        "nested-brace",
        "doubled-brace",
        "leading-digit",
        "hyphenated-name",
        "typed-name",
        "duplicate-name",
    ],
)
def test_invalid_templates_raise_value_error(
    compiler: cabc.Callable[[str], re.Pattern[str]], template: str
) -> None:
    """Malformed braces and invalid or duplicate names fail consistently."""
    with pytest.raises(ValueError, match=re.escape(template)) as exc_info:
        compiler(template)

    message = str(exc_info.value)
    assert template in message, "errors should identify the offending template"
    if template == "{}":
        assert message.startswith("Empty parameter name"), (
            "empty placeholders should keep their established message prefix"
        )


def test_literal_and_unicode_parameters_capture_nonempty_segments() -> None:
    """Named identifiers capture path segments around escaped literal text."""
    template = "/{_tenant}/rooms/{名}.json"
    match = compile_uri_template(template).fullmatch("/東京/rooms/report.json")

    assert match is not None, "Unicode and underscore-leading names should compile"
    assert match.groupdict() == {"_tenant": "東京", "名": "report"}, (
        "each placeholder should capture its corresponding path segment"
    )
    assert compile_uri_template(template).fullmatch("/東京/rooms/.json") is None, (
        "parameter captures must contain at least one character"
    )
    assert (
        compile_uri_template(template).fullmatch("/東京/rooms/reportxjson") is None
    ), "the dot in the suffix must match literally"


def test_multiple_and_adjacent_parameters_keep_segment_capture_semantics() -> None:
    """Multiple placeholders capture the intended nonempty path segments."""
    multiple = compile_uri_template("/{org}/rooms/{room_id}")
    match = multiple.fullmatch("/acme/rooms/r-42")
    assert match is not None, "the multi-parameter path should match"
    assert match.groupdict() == {"org": "acme", "room_id": "r-42"}, (
        "multiple placeholders should capture their intended segments"
    )

    adjacent = compile_uri_template("/{left}{right}").fullmatch("/ab")
    assert adjacent is not None, "adjacent placeholders should be accepted"
    assert adjacent.groupdict() == {"left": "a", "right": "b"}, (
        "adjacent placeholders should retain regex capture semantics"
    )


@pytest.mark.parametrize("compiler", [compile_uri_template, _compile_prefix_template])
def test_adjacent_parameter_runs_are_bounded(
    compiler: cabc.Callable[[str], re.Pattern[str]],
) -> None:
    """The compiler bounds ambiguous backtracking while supporting pairs."""
    with pytest.raises(ValueError, match="adjacent parameter limit"):
        compiler("/{first}{second}{third}")


def test_full_template_matches_optional_single_trailing_slash() -> None:
    """Trailing slash behavior remains identical for full patterns."""
    pattern = compile_uri_template("/foo/")

    assert pattern.fullmatch("/foo"), "full matching should accept no trailing slash"
    assert pattern.fullmatch("/foo/"), "full matching should accept one trailing slash"
    assert pattern.fullmatch("/foobar") is None, (
        "full matching should reject partial-segment paths"
    )
    assert pattern.fullmatch("/foo/bar") is None, (
        "full matching should reject paths with additional segments"
    )


def test_prefix_template_enforces_path_boundary() -> None:
    """A route prefix ends at a slash or path end, not a partial segment."""
    pattern = _compile_prefix_template("/foo")

    assert pattern.match("/foo"), "prefix matching should accept the route itself"
    assert pattern.match("/foo/bar"), "prefix matching should accept descendants"
    assert pattern.match("/foobar") is None, (
        "prefix matching should reject partial-segment paths"
    )


def test_compiled_patterns_remain_regular_expressions() -> None:
    """The compiler continues returning compiled standard-library patterns."""
    assert isinstance(compile_uri_template("/foo"), re.Pattern), (
        "the full compiler should return a compiled regular expression"
    )
    assert isinstance(_compile_prefix_template("/foo"), re.Pattern), (
        "the prefix compiler should return a compiled regular expression"
    )
