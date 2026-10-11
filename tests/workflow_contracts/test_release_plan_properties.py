"""Property tests for destination selection in release reconciliation."""

from __future__ import annotations

import operator

import hypothesis as hyp
import hypothesis.strategies as st

from scripts.release_assets import Source, mismatches, plan

# Small identifiers keep shrinking readable while still allowing arbitrary sets.
_NAME = st.integers(min_value=0, max_value=100).map(lambda n: f"pkg-{n}.whl")
_HOLDINGS = st.lists(
    st.tuples(_NAME, st.booleans(), st.booleans()),
    unique_by=operator.itemgetter(0),
    max_size=20,
)


@hyp.given(_HOLDINGS)
def test_plan_respects_precedence_and_missing_names(
    rows: list[tuple[str, bool, bool]],
) -> None:
    """Each filename has one source and is sent only to missing destinations."""
    names = [name for name, _, _ in rows]
    pypi = {name for name, present, _ in rows if present}
    github = {name for name, _, present in rows if present}
    result = plan(names, pypi, github)

    assert set(result.canonical) == set(names), "each input needs a source"
    for name, on_pypi, on_github in rows:
        if on_pypi:
            expected = Source.PYPI
        elif on_github:
            expected = Source.GITHUB
        else:
            expected = Source.LOCAL
        assert result.canonical[name] == expected, f"wrong source for {name}"
        assert (name in result.pypi_uploads) is not on_pypi, name
        assert (name in result.github_uploads) is not on_github, name
    assert result == plan(reversed(names), pypi, github), "order changed the plan"

    # After the planned uploads, no destination should need another upload.
    next_plan = plan(names, pypi | result.pypi_uploads, github | result.github_uploads)
    assert not next_plan.pypi_uploads, "PyPI rerun must have no uploads"
    assert not next_plan.github_uploads, "GitHub rerun must have no uploads"


@hyp.given(_HOLDINGS)
def test_digest_verification_rejects_disagreement(
    rows: list[tuple[str, bool, bool]],
) -> None:
    """Matching complete holdings verify; a differing digest must fail."""
    names = [name for name, _, _ in rows]
    pypi = dict.fromkeys(names, "original")
    github = dict.fromkeys(names, "original")
    assert not mismatches(names, pypi, github), "identical holdings must agree"
    if names:
        github[names[0]] = "different"
        assert mismatches(names, pypi, github), "different bytes must be rejected"
