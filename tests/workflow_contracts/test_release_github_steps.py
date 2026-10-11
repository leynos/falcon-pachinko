"""Behaviour of the release workflow's GitHub Release steps.

Each step runs its checked-in script against a ``gh`` stand-in that records
every call, so the assertions are about what the release asks GitHub to do: a
re-run reuses the tag's release instead of creating a second, a new release is
drafted and marked pre-release from the version check, the draft's existing
assets are snapshotted for the PyPI job, and the release is made visible last.
The reconciling upload and digest check are covered by
``test_release_reconciliation.py``.
"""

from __future__ import annotations

import json
import os
import sys
import typing as typ

import pytest

from tests.helpers.release_workflow import (
    fake_release,
    install_tool,
    link_scripts,
    run_bash,
    step_script,
)

if typ.TYPE_CHECKING:
    import pathlib as pth

_TAG = "v1.2.3"

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="The release workflow's steps run under Bash on Linux.",
)


def _run_with_fake_gh(
    tmp_path: pth.Path,
    script: str,
    *,
    view_status: int = 0,
    extra_env: dict[str, str] | None = None,
) -> tuple[int, str, list[list[str]]]:
    """Run ``script`` with a recording ``gh``; return status, stderr, and calls."""
    calls = tmp_path / "gh-calls"
    install_tool(
        tmp_path / "tools",
        "gh",
        'printf \'%s\\n\' "$*" >> "${GH_CALLS}"\n'
        'if [ "$1" = "api" ]; then\n'
        '  [ "$GH_API_STATUS" = 0 ] || exit "$GH_API_STATUS"\n'
        '  [ "$GH_RELEASE_PRESENT" = true ] && printf "%s\\n" "$GITHUB_REF_NAME"\n'
        "  exit 0\n"
        "fi\n"
        'if [ "$1 $2" = "release view" ]; then\n'
        '  case "$*" in\n'
        "    *--jq*) printf '%s\\n' \"${GH_IS_DRAFT}\";;\n"
        "    *--json\\ assets*) printf '{\"assets\": []}\\n';;\n"
        "  esac\n"
        '  exit "${GH_VIEW_STATUS}"\n'
        "fi",
    )
    env = {
        "PATH": os.pathsep.join((str(tmp_path / "tools"), os.defpath)),
        "GH_CALLS": str(calls),
        "GH_VIEW_STATUS": str(view_status),
        "GH_API_STATUS": "0",
        "GH_RELEASE_PRESENT": "true",
        "GH_REPO": "leynos/falcon-pachinko",
        "GH_IS_DRAFT": "true",
        "GITHUB_REF_NAME": _TAG,
        **(extra_env or {}),
    }
    completed = run_bash(script, tmp_path, env)
    recorded = (
        [line.split() for line in calls.read_text(encoding="utf-8").splitlines()]
        if calls.exists()
        else []
    )
    return completed.returncode, completed.stderr, recorded


def test_an_existing_release_is_reused(tmp_path: pth.Path) -> None:
    """A re-run finds the tag's release and creates nothing."""
    status, stderr, calls = _run_with_fake_gh(
        tmp_path,
        step_script("draft-release", "Create or reuse the draft release"),
        extra_env={"IS_PRERELEASE": "false"},
    )

    assert status == 0, stderr
    assert len(calls) == 1, calls
    assert calls[0][0] == "api", calls


@pytest.mark.parametrize(
    ("is_prerelease", "expect_prerelease_flag"),
    [("true", True), ("false", False)],
    ids=["prerelease", "final"],
)
def test_a_missing_release_is_created_as_a_draft(
    tmp_path: pth.Path, is_prerelease: str, *, expect_prerelease_flag: bool
) -> None:
    """A first run drafts the release and carries the pre-release status."""
    status, stderr, calls = _run_with_fake_gh(
        tmp_path,
        step_script("draft-release", "Create or reuse the draft release"),
        extra_env={"IS_PRERELEASE": is_prerelease, "GH_RELEASE_PRESENT": "false"},
    )

    assert status == 0, stderr
    assert len(calls) == 2, f"expected a lookup then a create, got {calls}"
    create = calls[1]
    assert create[:3] == ["release", "create", _TAG], create
    assert {"--draft", "--verify-tag", "--generate-notes"} <= set(create), (
        "the release must start as a draft of the pushed tag"
    )
    assert ("--prerelease" in create) is expect_prerelease_flag, (
        "the pre-release flag must follow the version check"
    )


def test_the_draft_check_reports_a_draft_release(tmp_path: pth.Path) -> None:
    """The upload decision is told to proceed while the release is a draft."""
    status, stderr, calls = _run_with_fake_gh(
        tmp_path,
        step_script("publish-release", "Note whether the release is a draft"),
        extra_env={"GITHUB_OUTPUT": str(tmp_path / "output")},
    )

    assert status == 0, stderr
    assert "is-draft=true" in (tmp_path / "output").read_text(encoding="utf-8"), (
        "Release contract failed"
    )
    assert calls == [
        ["release", "view", _TAG, "--json", "isDraft", "--jq", ".isDraft"]
    ], "Release contract failed"


def test_the_draft_check_reports_a_published_release(tmp_path: pth.Path) -> None:
    """Once the release is public the check says so, and nothing is uploaded."""
    status, stderr, _ = _run_with_fake_gh(
        tmp_path,
        step_script("publish-release", "Note whether the release is a draft"),
        extra_env={
            "GITHUB_OUTPUT": str(tmp_path / "output"),
            "GH_IS_DRAFT": "false",
        },
    )

    assert status == 0, stderr
    output = (tmp_path / "output").read_text(encoding="utf-8")
    assert "is-draft=false" in output, "Release contract failed"
    assert "nothing will be uploaded" in stderr, "Release contract failed"


def test_the_upload_declines_a_published_release(tmp_path: pth.Path) -> None:
    """A re-run against a public release asks GitHub to change nothing."""
    staged = tmp_path / "github-upload"
    staged.mkdir()
    (staged / "falcon-pachinko-v1.2.3.sigstore.json").write_bytes(b"bundle")

    status, stderr, calls = _run_with_fake_gh(
        tmp_path,
        step_script("publish-release", "Upload the assets the release lacks"),
        extra_env={"IS_DRAFT": "false"},
    )

    assert status == 0, stderr
    assert calls == [], "a published release must not receive another asset"


def test_the_snapshot_carries_the_release_bytes_for_this_runs_names(
    tmp_path: pth.Path,
) -> None:
    """Only assets sharing a name with this run's artefacts are downloaded."""
    fake = fake_release(tmp_path)
    link_scripts(tmp_path)
    publish = tmp_path / "dist" / "publish"
    publish.mkdir(parents=True)
    for name in ("p-1-py3-none-any.whl", "p-1.tar.gz"):
        (publish / name).write_bytes(b"rebuilt")
    for name in ("p-1.tar.gz", "p-0.9.tar.gz", "falcon-pachinko-v1.2.3.sigstore.json"):
        (fake.github / name).write_bytes(b"published")

    completed = run_bash(
        step_script("draft-release", "Snapshot the release assets"),
        tmp_path,
        fake.env(GITHUB_REF_NAME=_TAG),
    )

    assert completed.returncode == 0, completed.stderr
    state = tmp_path / "github-state"
    carried = {path.name: path.read_bytes() for path in (state / "files").iterdir()}
    assert carried == {"p-1.tar.gz": b"published"}, (
        "the PyPI job needs GitHub's bytes for exactly the names both share"
    )
    listed = json.loads((state / "assets.json").read_text(encoding="utf-8"))
    assert len(listed["assets"]) == 3, "the snapshot must list every asset"
    assert [call[:2] for call in fake.calls("gh")] == [
        ["release", "view"],
        ["release", "download"],
    ], "the snapshot only reads the release"


@pytest.mark.parametrize("is_prerelease", ["true", "false"])
def test_the_release_is_published_with_its_prerelease_status(
    tmp_path: pth.Path, is_prerelease: str
) -> None:
    """The draft is made visible, keeping the version check's status."""
    status, stderr, calls = _run_with_fake_gh(
        tmp_path,
        step_script("publish-release", "Publish the GitHub Release"),
        extra_env={"IS_PRERELEASE": is_prerelease},
    )

    assert status == 0, stderr
    assert calls == [
        ["release", "edit", _TAG, "--draft=false", f"--prerelease={is_prerelease}"]
    ], "Release contract failed"


def test_a_release_lookup_error_does_not_create_a_draft(tmp_path: pth.Path) -> None:
    """A failed listing must never be mistaken for an absent release."""
    status, _, calls = _run_with_fake_gh(
        tmp_path,
        step_script("draft-release", "Create or reuse the draft release"),
        extra_env={"IS_PRERELEASE": "false", "GH_API_STATUS": "1"},
    )
    assert status != 0, "failed lookup must stop the job"
    assert len(calls) == 1, calls
    assert calls[0][0] == "api", calls
