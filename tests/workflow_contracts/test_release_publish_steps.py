"""Behaviour of the release workflow's version check, collection, and PyPI steps.

The steps run their checked-in scripts in a scratch directory, with ``curl``
replaced by a stand-in serving scripted index responses, so the contract is
what the release actually executes: the tag must equal the declared version,
exactly one sdist travels with the wheels, the index lookup retries transient
failures and counts them, and files already on PyPI are skipped by name, with
nothing left meaning no upload at all.
"""

from __future__ import annotations

import json
import sys
import tomllib
import typing as typ

import pytest

from tests.helpers.ci_workflows import ROOT
from tests.helpers.release_workflow import (
    FakeRelease,
    fake_release,
    link_scripts,
    outputs,
    run_bash,
    run_version_check,
    step_script,
)

if typ.TYPE_CHECKING:
    import pathlib as pth
    import subprocess

_COLLECT_STEP = ("attest", "Collect release artefacts")
_INDEX_STEP = ("publish-pypi", "Fetch the PyPI index")
_SKIP_STEP = ("publish-pypi", "Skip artefacts already on PyPI")
_ARTEFACTS = ("p-1-py3-none-any.whl", "p-1.tar.gz")

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="The release workflow's steps run under Bash on Linux.",
)


def _touch(directory: pth.Path, *names: str) -> None:
    """Create artefact files named ``names`` under ``directory``."""
    directory.mkdir(parents=True, exist_ok=True)
    for name in names:
        (directory / name).write_bytes(name.encode())


@pytest.mark.parametrize(
    ("version", "expected_prerelease"),
    [
        ("0.2.0-beta1", "true"),
        ("0.2.0b1", "true"),
        ("1.0.0-rc.2", "true"),
        ("1.0.0a3", "true"),
        ("1.0.0.post1.dev2", "true"),
        ("1.2.3", "false"),
        ("1.2.3.post1", "false"),
        ("1.2.3+rc1", "false"),
        ("10.20.30", "false"),
    ],
)
def test_a_matching_tag_passes_and_reports_its_prerelease_status(
    tmp_path: pth.Path, version: str, expected_prerelease: str
) -> None:
    """The tag-derived version must equal the declared one, in either spelling."""
    completed, written = run_version_check(tmp_path, version, version)

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert written == {"version": version, "prerelease": expected_prerelease}, (
        f"{version} must be reported as prerelease={expected_prerelease}"
    )


@pytest.mark.parametrize(
    ("declared", "tag_version"),
    [("1.2.2", "1.2.3"), ("0.2.0-beta1", "0.2.0b1"), ("0.2.0-beta1", "0.2.0")],
    ids=["older-manifest", "respelled-prerelease", "dropped-prerelease"],
)
def test_a_mismatched_tag_fails_before_anything_is_published(
    tmp_path: pth.Path, declared: str, tag_version: str
) -> None:
    """A tag naming another version stops the release with an annotation."""
    completed, written = run_version_check(tmp_path, declared, tag_version)

    assert completed.returncode != 0, "a mismatched tag must fail the release"
    assert "::error file=pyproject.toml,title=Tag/pyproject.toml mismatch::" in (
        completed.stdout
    ), "Release contract failed"
    assert not written, "a failed check must not report a prerelease status"


def test_the_distribution_is_the_name_the_release_publishes() -> None:
    """The declared name is the one the index lookup and the release URL use."""
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text("utf-8"))
    workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text("utf-8")

    assert pyproject["project"]["name"] == "falcon-pachinko", "Release contract failed"
    assert "https://pypi.org/simple/falcon-pachinko/" in workflow, (
        "Release contract failed"
    )
    assert "https://pypi.org/project/falcon-pachinko/" in workflow, (
        "Release contract failed"
    )


@pytest.mark.parametrize(
    "wheels",
    [
        ("p-1-py3-none-any.whl",),
        ("p-1-py3-none-any.whl", "p-1-cp313-cp313-linux.whl"),
    ],
    ids=["single-wheel", "multiple-wheels"],
)
def test_collection_gathers_the_sdist_with_every_wheel(
    tmp_path: pth.Path, wheels: tuple[str, ...]
) -> None:
    """Every wheel travels with the single sdist, preserving its bytes."""
    expected = (*wheels, "p-1.tar.gz")
    _touch(tmp_path / "dist", *expected)

    completed = run_bash(step_script(*_COLLECT_STEP), tmp_path)

    assert completed.returncode == 0, completed.stderr
    publish = tmp_path / "dist" / "publish"
    collected = sorted(path.name for path in publish.iterdir())
    assert collected == sorted(expected), "every wheel and the sdist must be staged"
    for name in expected:
        assert (publish / name).read_bytes() == name.encode(), (
            f"collection must preserve the bytes of {name}"
        )


@pytest.mark.parametrize(
    "sdists",
    [(), ("p-1.tar.gz", "p-2.tar.gz")],
    ids=["no-sdist", "two-sdists"],
)
def test_collection_fails_without_exactly_one_sdist(
    tmp_path: pth.Path, sdists: tuple[str, ...]
) -> None:
    """A release that lost its sdist, or gained a second, fails before upload."""
    _touch(tmp_path / "dist", "p-1-py3-none-any.whl", *sdists)

    completed = run_bash(step_script(*_COLLECT_STEP), tmp_path)

    assert completed.returncode != 0, "the release needs exactly one sdist"
    assert "Expected exactly one sdist and at least one wheel" in completed.stderr, (
        "Release contract failed"
    )


def _fetch_and_skip(
    tmp_path: pth.Path, statuses: str, published: tuple[str, ...] = ()
) -> tuple[FakeRelease, subprocess.CompletedProcess[str], dict[str, str]]:
    """Run the index fetch, then the skip step if the fetch succeeded."""
    fake = fake_release(tmp_path)
    link_scripts(tmp_path)
    _touch(tmp_path / "dist" / "publish", *_ARTEFACTS)
    _touch(fake.pypi, *published)
    (tmp_path / "github-state" / "files").mkdir(parents=True)
    (tmp_path / "github-state" / "assets.json").write_text('{"assets": []}', "utf-8")
    env = fake.env(FAKE_CURL_STATUSES=statuses)
    completed = run_bash(step_script(*_INDEX_STEP), tmp_path, env)
    if completed.returncode == 0:
        completed = run_bash(step_script(*_SKIP_STEP), tmp_path, env)
    return fake, completed, outputs(tmp_path / "github-output")


def _remaining(tmp_path: pth.Path) -> list[str]:
    """Return the artefact names left for the PyPI upload."""
    return sorted(path.name for path in (tmp_path / "dist" / "pypi").iterdir())


@pytest.mark.parametrize(
    ("published", "remaining", "has_remaining"),
    [
        (_ARTEFACTS[1:], [_ARTEFACTS[0]], "true"),
        (_ARTEFACTS, [], "false"),
    ],
    ids=["uploads-remaining", "nothing-left"],
)
def test_artefacts_already_on_pypi_are_skipped_by_name(
    tmp_path: pth.Path,
    published: tuple[str, ...],
    remaining: list[str],
    has_remaining: str,
) -> None:
    """Only unpublished names remain, and the step says whether any do."""
    _, completed, written = _fetch_and_skip(tmp_path, "200", published)

    assert completed.returncode == 0, completed.stderr
    assert _remaining(tmp_path) == remaining, "published names must be dropped"
    assert "::notice::Skipping p-1.tar.gz: already on PyPI" in completed.stdout, (
        "Release contract failed"
    )
    assert written == {"attempts": "1", "status": "200", "remaining": has_remaining}, (
        "Release contract failed"
    )


def test_a_project_absent_from_pypi_reads_as_an_empty_index(
    tmp_path: pth.Path,
) -> None:
    """A 404 is a first release: nothing is skipped and every file uploads."""
    _, completed, written = _fetch_and_skip(tmp_path, "404")

    assert completed.returncode == 0, completed.stderr
    index = json.loads((tmp_path / "pypi-index.json").read_text(encoding="utf-8"))
    assert index == {"files": []}, "Release contract failed"
    assert _remaining(tmp_path) == list(_ARTEFACTS), "Release contract failed"
    assert written == {"attempts": "1", "status": "404", "remaining": "true"}, (
        "Release contract failed"
    )


def test_a_transient_failure_is_retried_and_counted(tmp_path: pth.Path) -> None:
    """A 503 then a 429 then a 200 succeeds on the third attempt."""
    fake, completed, written = _fetch_and_skip(tmp_path, "503,429,200")

    assert completed.returncode == 0, completed.stderr
    assert written["attempts"] == "3", "the retry count must be reported"
    assert written["status"] == "200", "Release contract failed"
    assert len(fake.calls("curl")) == 3, "Release contract failed"


@pytest.mark.parametrize(
    ("statuses", "attempts"),
    [("500", "6"), ("000", "6"), ("403", "1")],
    ids=["server-error", "network-error", "client-error"],
)
def test_a_failed_index_lookup_stops_the_upload(
    tmp_path: pth.Path, statuses: str, attempts: str
) -> None:
    """A persistent 5xx or network error, or any other 4xx, fails the job."""
    fake, completed, written = _fetch_and_skip(tmp_path, statuses)

    assert completed.returncode != 0, f"HTTP {statuses} must fail the lookup"
    assert f"PyPI simple index returned HTTP {statuses}." in completed.stderr, (
        "Release contract failed"
    )
    assert written == {"attempts": attempts, "status": statuses}, (
        "Release contract failed"
    )
    assert len(fake.calls("curl")) == int(attempts), "the loop must stay bounded"
    assert sorted(p.name for p in (tmp_path / "dist" / "publish").iterdir()) == list(
        _ARTEFACTS
    ), "nothing may be skipped"


@pytest.mark.parametrize("listing", [None, ""])
def test_carried_verification_distinguishes_missing_and_empty_list(
    tmp_path: pth.Path, listing: str | None
) -> None:
    """No carry needs no provenance; an absent snapshot must fail closed."""
    fake = fake_release(tmp_path)
    state = tmp_path / "github-state"
    state.mkdir()
    if listing is not None:
        (state / "carryover.txt").write_text(listing, encoding="utf-8")
    completed = run_bash(
        step_script("draft-release", "Verify carried asset provenance"),
        tmp_path,
        fake.env(GITHUB_REF_NAME="v1.2.3"),
    )
    assert (completed.returncode == 0) is (listing is not None), completed.stderr
    assert not fake.calls("gh"), "no assets should trigger no network verification"
