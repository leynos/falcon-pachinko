r"""Properties of the CodeScene scan, over generated workflow documents.

The contracts beside this file each ask one question of one document. They pin
the cases that were got wrong and say nothing about the space between them, and
the scan's job is to read *arbitrary* nested YAML: a credential can sit at any
depth, under any key, inside any list.

Four invariants hold:

* the walk reaches every scalar in a document, so nothing can hide by being
  nested more deeply than the fixtures happen to go;
* a credential planted anywhere in an otherwise innocent document is found;
* a document carrying no marker at any depth is cleared; and
* the action marker answers to the ``uses`` key alone, wherever the same text
  appears elsewhere.

The first is the one the others rest on. A walk that stopped at some depth
would clear every document deeper than that while every fixture above still
passed.

Run with:

    uv run pytest tests/workflow_contracts
"""

from __future__ import annotations

import hypothesis as hyp
import hypothesis.strategies as st

from .codescene_scan import (
    ACTION_DESCRIPTION,
    MARKERS,
    _walk,
    references_in,
)

#: Scalars that trip no marker. Kept short and alphabetic so a generated
#: document cannot accidentally contain "codescene" or "cs-coverage".
innocent_scalars = st.text(
    alphabet=st.sampled_from("abcdefghijklmnopqrstuvw -_/."), max_size=12
)
innocent_keys = st.text(
    alphabet=st.sampled_from("abcdefghijklmnopqrstuvw_-"), min_size=1, max_size=8
)

documents = st.recursive(
    innocent_scalars | st.booleans() | st.integers(min_value=-99, max_value=99),
    lambda children: (
        st.dictionaries(innocent_keys, children, max_size=4)
        | st.lists(children, max_size=4)
    ),
    max_leaves=12,
)


def _scalar_count(value: object) -> int:
    """Count the scalars a document holds.

    Parameters
    ----------
    value : object
        A parsed document.

    Returns
    -------
    int
        The number of leaves, counting a mapping's keys as leaves too.
    """
    match value:
        case dict():
            return sum(1 + _scalar_count(entry) for entry in value.values())
        case list():
            return sum(_scalar_count(entry) for entry in value)
        case _:
            return 1


@hyp.given(document=documents)
def test_the_walk_reaches_every_scalar(document: object) -> None:
    """Reach every leaf, at any depth.

    Every other rule here clears a document by finding nothing in it, so a walk
    that stopped early would clear whatever sat below the cut-off while every
    fixture above it still passed. Counting is the only way to see that from
    outside.
    """
    reached = list(_walk(document, "fixture.yml"))

    assert len(reached) == _scalar_count(document), (
        f"the walk reached {len(reached)} scalars of {_scalar_count(document)}"
    )


@hyp.given(document=documents, marker=st.sampled_from(sorted(MARKERS)))
def test_a_marker_planted_anywhere_is_found(document: object, marker: str) -> None:
    """Find a credential or a command wherever it is written.

    Neither is scoped to a key, because neither has one place it must appear:
    a credential can be declared at workflow, job or step scope or passed as an
    input, and a command can be buried in any shell script.
    """
    planted = {"jobs": {"gate": {"env": {"X": MARKERS[marker]}, "rest": document}}}

    found = references_in(planted, "fixture.yml")

    assert any(entry.startswith(marker) for entry in found), (
        f"{marker} planted in an arbitrary document was not found: {found}"
    )


@hyp.given(document=documents)
def test_an_innocent_document_is_cleared(document: object) -> None:
    """Clear a document that names nothing, however deeply nested.

    A scan reporting an interaction in every document would pass its marker
    fixtures and fail every real workflow, which is the opposite defect and
    just as invisible from a fixture.
    """
    assert references_in(document, "fixture.yml") == []


@hyp.given(key=innocent_keys)
def test_the_action_marker_answers_to_the_uses_key_alone(key: str) -> None:
    """Report a CodeScene action reference, and only under ``uses``.

    The same text in a step name or a description is prose about the rule, not
    an invocation of it. Refusing prose would make the rule impossible to
    document in the workflow it governs.
    """
    reference = "leynos/shared-actions/.github/actions/codescene-x@" + "0" * 40
    hyp.assume(key != "uses")

    under_uses = references_in(
        {"jobs": {"g": {"steps": [{"uses": reference}]}}}, "fixture.yml"
    )
    elsewhere = references_in(
        {"jobs": {"g": {"steps": [{key: reference}]}}}, "fixture.yml"
    )

    assert any(entry.startswith(ACTION_DESCRIPTION) for entry in under_uses), (
        f"an action reference under uses must be found; got {under_uses}"
    )
    assert not [entry for entry in elsewhere if entry.startswith(ACTION_DESCRIPTION)], (
        f"the same text under {key!r} is not an invocation; got {elsewhere}"
    )
