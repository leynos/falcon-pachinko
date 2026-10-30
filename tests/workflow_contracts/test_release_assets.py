"""Unit tests for :mod:`scripts.release_assets`.

The release workflow's own steps are exercised end to end in
``test_release_reconciliation.py``; these tests pin the small decisions that
run decides, so a failure names the rule rather than the whole pipeline.
"""

from __future__ import annotations

import typing as typ

import pytest

from scripts import release_assets
from scripts.release_assets import PypiFile, Source, State

if typ.TYPE_CHECKING:
    import pathlib as pth


def _state(
    publish: pth.Path,
    *,
    pypi: dict[str, str] | None = None,
    github: dict[str, str | None] | None = None,
) -> State:
    """Build a ``State``; a digest of ``"canonical"`` is hashed from ``publish``.

    Any other value is used verbatim, so a test can state a digest that no
    local file holds.

    Returns
    -------
    State
        The constructed release state.
    """
    declared = pypi or {}
    return State(
        publish=publish,
        pypi={
            name: PypiFile(
                sha256=(
                    release_assets.sha256_of(publish / name)
                    if digest == "canonical"
                    else digest
                ),
                url=f"https://files.test/{name}",
            )
            for name, digest in declared.items()
        },
        github=github or {},
    )


def _built(publish: pth.Path, *names: str) -> pth.Path:
    """Write this run's build of each name and return the directory."""
    publish.mkdir(parents=True, exist_ok=True)
    for name in names:
        (publish / name).write_bytes(f"built {name}".encode())
    return publish


def test_a_first_release_attests_the_bytes_it_ships(tmp_path: pth.Path) -> None:
    """Nothing is published yet, so this run's bytes are canonical everywhere."""
    publish = _built(tmp_path / "publish", "p-1.whl", "p-1.tar.gz")

    assert release_assets.attests_canonical_bytes(_state(publish)), (
        "Release contract must hold"
    )


def test_a_rerun_whose_rebuild_differs_attests_nothing(tmp_path: pth.Path) -> None:
    """PyPI's immutable bytes stay canonical, so the rebuild is not the subject."""
    publish = _built(tmp_path / "publish", "p-1.whl", "p-1.tar.gz")
    state = _state(publish, pypi={"p-1.whl": "canonical"})
    (publish / "p-1.whl").write_bytes(b"rebuilt differently")

    assert not release_assets.attests_canonical_bytes(state), (
        "Release contract must hold"
    )


def test_a_rerun_whose_rebuild_matches_still_attests_it(tmp_path: pth.Path) -> None:
    """A byte-identical rebuild is the published artefact, whatever its source."""
    publish = _built(tmp_path / "publish", "p-1.whl")
    state = _state(publish, pypi={"p-1.whl": "canonical"})

    assert release_assets.attests_canonical_bytes(state), "Release contract must hold"


def test_an_unreported_github_digest_attests_nothing(tmp_path: pth.Path) -> None:
    """An asset GitHub will not vouch for cannot be shown to match this build."""
    publish = _built(tmp_path / "publish", "p-1.whl")
    state = _state(publish, github={"p-1.whl": None})

    assert not release_assets.attests_canonical_bytes(state), (
        "Release contract must hold"
    )


def test_the_canonical_digest_comes_from_the_source_the_plan_chose(
    tmp_path: pth.Path,
) -> None:
    """Each name reports its plan's source and that source's digest."""
    publish = _built(tmp_path / "publish", "a.whl", "b.whl", "c.whl")
    state = _state(
        publish,
        pypi={"a.whl": "canonical"},
        github={"b.whl": release_assets.sha256_of(publish / "b.whl")},
    )

    assert release_assets.canonical_digest(state, "a.whl") == (
        Source.PYPI,
        release_assets.sha256_of(publish / "a.whl"),
    ), "Release contract must hold"
    assert release_assets.canonical_digest(state, "b.whl") == (
        Source.GITHUB,
        release_assets.sha256_of(publish / "b.whl"),
    ), "Release contract must hold"
    assert release_assets.canonical_digest(state, "c.whl") == (
        Source.LOCAL,
        release_assets.sha256_of(publish / "c.whl"),
    ), "Release contract must hold"


@pytest.mark.parametrize(
    ("expected_names", "listed", "missing"),
    [
        ({"a.whl"}, {"a.whl"}, []),
        ({"a.whl", "b.whl"}, {"a.whl"}, ["b.whl"]),
        (set(), set(), []),
        ({"a.whl"}, set(), ["a.whl"]),
    ],
    ids=["all-present", "one-missing", "nothing-expected", "all-missing"],
)
def test_the_missing_list_names_what_the_index_does_not_yet_show(
    expected_names: set[str], listed: set[str], missing: list[str]
) -> None:
    """The poll is over exactly the names this run must see appear."""
    assert release_assets.missing_from_index(expected_names, listed) == missing, (
        "Release contract must hold"
    )


def test_verification_rejects_empty_distribution_set(tmp_path: pth.Path) -> None:
    """Verification cannot publish an empty release."""
    publish = _built(tmp_path / "publish")
    bundles = tmp_path / "bundles"
    bundles.mkdir()
    problems = release_assets.verify(_state(publish), bundles)
    assert problems, "an empty distribution set must fail verification"


def test_malformed_pypi_entry_reports_its_source(tmp_path: pth.Path) -> None:
    """An index entry without hashes reports the file instead of a bare key."""
    index = tmp_path / "index.json"
    index.write_text(
        '{"files": [{"filename": "p.whl", "url": "p.whl"}]}', encoding="utf-8"
    )
    with pytest.raises(
        release_assets.ReleaseAssetError, match=r"index\.json.*malformed"
    ):
        release_assets.read_pypi_index(index)


def test_malformed_github_entry_reports_its_source(tmp_path: pth.Path) -> None:
    """A nameless asset is rejected with the listing filename."""
    assets = tmp_path / "assets.json"
    assets.write_text('{"assets": [{}]}', encoding="utf-8")
    with pytest.raises(
        release_assets.ReleaseAssetError, match=r"assets\.json.*malformed"
    ):
        release_assets.read_github_assets(assets)
