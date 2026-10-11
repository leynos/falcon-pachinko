"""Tests for URI-template compilation."""

from __future__ import annotations

import re
import string
import typing as typ

import pytest
from hypothesis import given
from hypothesis import strategies as st

from falcon_pachinko._uri_template import (
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


@given(
    prefix=st.text(alphabet=string.ascii_lowercase, max_size=12),
    metacharacter=st.sampled_from(tuple(".+?*[]()^$|\\")),
    suffix=st.text(alphabet=string.ascii_lowercase, max_size=12),
)
def test_generated_literal_runs_remain_literal(
    prefix: str, metacharacter: str, suffix: str
) -> None:
    """Both compilers preserve ordinary and regex-special literal text."""
    segment = f"{prefix}{metacharacter}{suffix}"
    template = f"/{segment}"
    full_pattern = compile_uri_template(template)
    prefix_pattern = _compile_prefix_template(template)

    assert full_pattern.fullmatch(template), "full matching should preserve the text"
    assert full_pattern.fullmatch(f"{template}/"), (
        "full matching should retain optional trailing slashes"
    )
    assert full_pattern.fullmatch(f"{template}suffix") is None, (
        "full matching should reject a literal near-miss"
    )
    assert prefix_pattern.match(f"{template}/child"), (
        "prefix matching should preserve the exact literal segment"
    )
    assert prefix_pattern.match(f"{template}suffix") is None, (
        "prefix matching should reject an expanded or partial literal"
    )


def _make_identifier_route_case(
    name_suffix: str,
    value: str,
    layout: typ.Literal["adjacent", "single"],
) -> tuple[str, str, dict[str, str]]:
    """Build a route case whose generated names satisfy Python's identifier rule."""
    if layout == "adjacent":
        names = (f"left_{name_suffix}", f"right_{name_suffix}")
        return (
            f"/rooms/{{{names[0]}}}{{{names[1]}}}",
            f"/rooms/{value}x",
            {names[0]: value, names[1]: "x"},
        )

    name = f"room_{name_suffix}"
    return f"/rooms/{{{name}}}", f"/rooms/{value}", {name: value}


def _assert_generated_route_matches(
    template: str, route_path: str, expected: dict[str, str]
) -> None:
    """Check generated full and prefix matches preserve nonempty captures."""
    full_pattern = compile_uri_template(template)
    prefix_pattern = _compile_prefix_template(template)
    full_match = full_pattern.fullmatch(route_path)
    trailing_slash_match = full_pattern.fullmatch(f"{route_path}/")
    prefix_match = prefix_pattern.match(f"{route_path}/child")

    assert full_match is not None, "full matching should capture the generated value"
    assert full_match.groupdict() == expected, (
        "full matching should preserve the placeholder name and captured value"
    )
    assert trailing_slash_match is not None, (
        "full matching should preserve its optional trailing slash"
    )
    assert trailing_slash_match.groupdict() == expected, (
        "trailing-slash matching should preserve the captured values"
    )
    assert prefix_match is not None, (
        "prefix matching should capture the generated value"
    )
    assert prefix_match.groupdict() == expected, (
        "prefix matching should preserve the placeholder name and captured value"
    )
    assert prefix_match.end() == len(route_path) + 1, (
        "prefix matching should consume the route and separator only"
    )
    assert prefix_pattern.match("/rooms//child") is None, (
        "prefix placeholders should continue rejecting empty segments"
    )


@given(
    name_suffix=st.text(alphabet=string.ascii_lowercase + string.digits, max_size=12),
    layout=st.sampled_from(("adjacent", "single")),
    value=st.text(
        alphabet=st.characters(
            blacklist_characters="/", blacklist_categories=("Cc", "Cs")
        ),
        min_size=1,
        max_size=24,
    ),
)
def test_generated_identifier_placeholders_capture_nonempty_segments(
    name_suffix: str, layout: typ.Literal["adjacent", "single"], value: str
) -> None:
    """Generated identifiers capture nonempty values in both supported layouts."""
    template, route_path, expected = _make_identifier_route_case(
        name_suffix, value, layout
    )
    _assert_generated_route_matches(template, route_path, expected)


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


@pytest.mark.parametrize(
    ("compiler", "path", "remainder"),
    [
        (compile_uri_template, "/abcdef/ok", ""),
        (_compile_prefix_template, "/abcdef/ok/child", "child"),
    ],
    ids=["full", "prefix"],
)
def test_adjacent_parameters_before_another_segment_bound_backtracking(
    compiler: cabc.Callable[[str], re.Pattern[str]], path: str, remainder: str
) -> None:
    """A terminal adjacent pair preserves greedy captures without nested retries."""
    pattern = compiler("/{left}{right}/ok")
    match = pattern.match(path)

    assert match is not None, (
        "adjacent placeholders before another segment should match"
    )
    assert match.groupdict() == {"left": "abcde", "right": "f"}, (
        "the greedy first placeholder should leave only the final character"
    )
    assert path[match.end() :] == remainder, (
        "full and prefix matching should retain their path consumption semantics"
    )
    assert "(?P<right>[^/])" in pattern.pattern, (
        "the second adjacent capture must not retry variable-length splits"
    )
    assert pattern.match(f"/{'x' * 8000}/no") is None, (
        "a long segment with a nonmatching successor should be rejected"
    )
    assert pattern.match("/a/ok") is None, (
        "both adjacent placeholders must still capture a nonempty value"
    )


@pytest.mark.parametrize("compiler", [compile_uri_template, _compile_prefix_template])
def test_ambiguous_parameter_segments_are_rejected(
    compiler: cabc.Callable[[str], re.Pattern[str]],
) -> None:
    """The compiler permits only a terminal adjacent pair per segment."""
    for template in (
        "/{first}{second}{third}",
        "/{first}a{second}a{third}z",
        "/{first}a{second}",
        "/{first}{second}.json",
    ):
        with pytest.raises(ValueError, match=re.escape(template)):
            compiler(template)


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
