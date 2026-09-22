r"""Properties of the label readers, over generated declarations.

The fixture-driven contracts beside this file each ask one question of one
document. They pin the cases that were got wrong, and they say nothing about
the space between those cases. These modules parse arbitrary folding
whitespace, arbitrary guards and arbitrary matrix shapes, so the invariants
are stated over generated input instead.

Four hold:

* collapsing folding whitespace is idempotent and leaves no run of it;
* it absorbs only the characters YAML folds, and in particular leaves the
  file separators U+001C to U+001F alone, which Python's ``\s`` would take;
* an expression written with any folding survives a round trip through the
  parser with its guard and both arms intact; and
* the labels a job resolves are exactly those declared under the matrix keys
  its ``runs-on`` names, and never those under a key it does not.

Run with:

    uv run pytest tests/workflow_contracts
"""

from __future__ import annotations

import hypothesis as hyp
import hypothesis.strategies as st
import pytest

from .runner_labels import (
    collapse_label_whitespace,
    job_labels,
    runner_expression,
)
from .runner_matrix import CompositeMatrixDeclarationError

#: The characters a YAML folded scalar can leave in a label.
FOLDING_WHITESPACE = " \t\n\r"
#: Characters Python's ``\\s`` treats as whitespace and a runner label does
#: not. A label carrying one is wrong, and collapsing it would hide that.
FILE_SEPARATORS = "\x1c\x1d\x1e\x1f"

labels = st.text(
    alphabet=st.sampled_from("abcdefghijklmnopqrstuvwxyz0123456789-"),
    min_size=1,
    max_size=24,
)
guards = st.text(
    alphabet=st.sampled_from("abcdefghijklmnopqrstuvwxyz._"),
    min_size=1,
    max_size=40,
)
folding = st.text(alphabet=st.sampled_from(FOLDING_WHITESPACE), min_size=1, max_size=4)
matrix_key_names = st.text(
    alphabet=st.sampled_from("abcdefghijklmnopqrstuvwxyz_"), min_size=1, max_size=8
)


@hyp.given(st.text(alphabet=st.sampled_from(FOLDING_WHITESPACE + "ab-"), max_size=40))
def test_collapsing_is_idempotent_and_leaves_no_run(raw: str) -> None:
    """Collapse to a fixed point with no folding whitespace run left.

    Contracts that ask what a label *says* read it through this helper, so a
    second pass must be a no-op; otherwise two readers of the same label can
    disagree about whether it matches.
    """
    once = collapse_label_whitespace(raw)

    assert collapse_label_whitespace(once) == once
    assert "  " not in once
    assert not any(character in once for character in "\t\n\r")


@hyp.given(
    prefix=st.text(alphabet=st.sampled_from("ab"), min_size=1, max_size=4),
    separator=st.sampled_from(FILE_SEPARATORS),
    suffix=st.text(alphabet=st.sampled_from("ab"), min_size=1, max_size=4),
)
def test_collapsing_leaves_the_file_separators_alone(
    prefix: str, separator: str, suffix: str
) -> None:
    r"""Absorb only what YAML folds.

    Python's ``\s`` also matches U+001C to U+001F. Those are not whitespace
    to a runner label: a label carrying one is malformed, and quietly
    absorbing it would make the malformed label compare equal to the correct
    one and pass every contract here.
    """
    collapsed = collapse_label_whitespace(f"{prefix}{separator}{suffix}")

    assert collapsed == f"{prefix}{separator}{suffix}"


@st.composite
def written_expressions(draw: st.DrawFn) -> tuple[str, str, str, str]:
    """Draw a conditional label together with the parts it must parse to.

    A folded scalar puts whitespace wherever it joined two lines, and the
    author chooses where those lines break, so one label has many spellings.
    The strategy draws a spelling and reports what it denotes.

    Parameters
    ----------
    draw : st.DrawFn
        Hypothesis's draw function.

    Returns
    -------
    tuple[str, str, str, str]
        The label as written, then its guard and its two arms.
    """
    guard = draw(guards)
    when_true = draw(labels)
    when_false = draw(labels)
    folds = draw(st.lists(folding, min_size=6, max_size=6))
    lead, trail = draw(folding), draw(folding)
    written = (
        f"{lead}${{{{{folds[0]}{guard}{folds[1]}&&{folds[2]}'{when_true}'"
        f"{folds[3]}||{folds[4]}'{when_false}'{folds[5]}}}}}{trail}"
    )
    return written, guard, when_true, when_false


@hyp.given(drawn=written_expressions())
def test_an_expression_survives_any_folding(drawn: tuple[str, str, str, str]) -> None:
    """Parse the same guard and arms however the scalar was folded.

    Every spelling denotes one label, so all of them must parse identically;
    only the contract that reads the raw text may see the difference.
    """
    written, guard, when_true, when_false = drawn

    parsed = runner_expression(written)

    assert parsed.guard == guard
    assert parsed.when_true == when_true
    assert parsed.when_false == when_false
    assert parsed.raw == written


@hyp.given(
    matrix=st.dictionaries(
        matrix_key_names,
        st.lists(labels, min_size=1, max_size=4),
        min_size=2,
        max_size=4,
    ),
    data=st.data(),
)
def test_a_job_resolves_exactly_the_axis_its_declaration_names(
    matrix: dict[str, list[str]], data: st.DataObject
) -> None:
    """Resolve the named axis and no other.

    This is the invariant behind the registry equality. Resolving an axis the
    declaration does not name would demand a registration for a runner no job
    can request; failing to resolve the one it names would let a paid runner
    go unregistered, which is the case the registry exists to catch.
    """
    named = data.draw(st.sampled_from(sorted(matrix)))
    job = {"runs-on": f"${{{{ matrix.{named} }}}}", "strategy": {"matrix": matrix}}

    resolved = job_labels("ci.yml", "build", job)

    assert resolved == set(matrix[named])


@hyp.given(
    matrix=st.dictionaries(
        matrix_key_names,
        st.lists(labels, min_size=1, max_size=4),
        min_size=2,
        max_size=4,
    ),
    data=st.data(),
)
def test_a_declaration_naming_several_axes_is_refused(
    matrix: dict[str, list[str]], data: st.DataObject
) -> None:
    """Refuse a composed declaration, whichever axes it names.

    GitHub renders it into one label per combination, which is none of the
    axis values, so a union of the axes would be the wrong answer.
    """
    named = data.draw(
        st.lists(st.sampled_from(sorted(matrix)), min_size=2, unique=True)
    )
    job = {
        "runs-on": " ".join(f"${{{{ matrix.{key} }}}}" for key in named),
        "strategy": {"matrix": matrix},
    }

    with pytest.raises(CompositeMatrixDeclarationError):
        job_labels("ci.yml", "build", job)
