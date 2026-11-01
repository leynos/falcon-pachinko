"""Prove action reference contracts reject mutable and malformed declarations."""

from __future__ import annotations

import typing as typ

if typ.TYPE_CHECKING:
    import pathlib

import hypothesis as hyp
import hypothesis.strategies as st
import pytest
import yaml

from .action_references import (
    _is_pinned,
    _manifest_references,
    _workflow_references,
    action_references,
    validate_reference,
)
from .workflow_support import (
    ACTION_MANIFESTS,
    REPOSITORY,
    ActionManifestSource,
    NotAListError,
    NotAMappingError,
    UnparsableWorkflowError,
    UnreadableWorkflowError,
    UnsupportedUsesError,
    WorkflowSource,
    _raise_unreadable_directory,
)


def sources(
    directory: pathlib.Path, workflow: object, action: object
) -> tuple[WorkflowSource, ActionManifestSource]:
    """Write one workflow and action manifest for a contract example."""
    workflows = directory / "workflows"
    workflows.mkdir()
    (workflows / "ci.yml").write_text(yaml.safe_dump(workflow), encoding="utf-8")
    actions = directory / "actions"
    (actions / "example").mkdir(parents=True)
    (actions / "example" / "action.yml").write_text(
        yaml.safe_dump(action), encoding="utf-8"
    )
    return WorkflowSource(workflows, directory / "config.yml"), ActionManifestSource(
        actions
    )


@pytest.mark.parametrize(
    "ref",
    [
        "v1",
        "v1.2.3",
        "main",
        "1234567",
        "A" * 40,
        "a" * 39,
        "abc",
        "docker://image:latest",
    ],
)
def test_mutable_references_are_rejected(ref: str) -> None:
    """Reject mutable, malformed and abbreviated external references."""
    value = ref if ref.startswith("docker://") else f"owner/repo@{ref}"
    with pytest.raises(UnsupportedUsesError):
        validate_reference("test", value)


@hyp.given(st.text(alphabet="0123456789abcdef", min_size=40, max_size=40))
def test_full_shas_are_accepted(sha: str) -> None:
    """All lowercase full commit identifiers are immutable references."""
    validate_reference("test", f"owner/repo/path@{sha}")


@hyp.given(st.text(alphabet="ghijklmnopqrstuvwxyz/-", min_size=1, max_size=30))
def test_generated_tags_and_branches_are_rejected(ref: str) -> None:
    """Tag-like and branch-like refs cannot satisfy the full SHA rule."""
    with pytest.raises(UnsupportedUsesError):
        validate_reference("test", f"owner/repo@{ref}")


def test_discovery_and_references(tmp_path: pathlib.Path) -> None:
    """Read reusable jobs, steps and composite steps, ignoring run-only steps."""
    sha = "a" * 40
    workflow, actions = sources(
        tmp_path,
        {
            "jobs": {
                "reuse": {"uses": f"owner/repo/.github/workflows/a.yml@{sha}"},
                "run": {"steps": [{"run": "true"}, {"uses": "./local"}]},
            }
        },
        {"runs": {"using": "composite", "steps": [{"uses": f"owner/repo@{sha}"}]}},
    )
    refs = list(action_references(workflow, actions))
    assert len(refs) == 3, "all three declarations must be read"
    for site, ref in refs:
        validate_reference(site, ref)


def test_nested_action_manifests_are_discovered(tmp_path: pathlib.Path) -> None:
    """Discover composite manifests in nested local-action directories."""
    workflow, actions = sources(
        tmp_path,
        {"jobs": {}},
        {"runs": {"using": "node20"}},
    )
    nested = actions.action_dir / "group" / "nested"
    nested.mkdir(parents=True)
    (nested / "action.yaml").write_text(
        yaml.safe_dump({
            "runs": {
                "using": "composite",
                "steps": [{"uses": f"owner/repo@{'a' * 40}"}],
            }
        }),
        encoding="utf-8",
    )

    refs = list(action_references(workflow, actions))

    assert "group/nested/action.yaml" in actions.names(), (
        "nested action manifests must be discovered"
    )
    assert (
        "group/nested/action.yaml:runs.steps[0]",
        f"owner/repo@{'a' * 40}",
    ) in refs, "nested action references must be yielded"


def test_non_composite_docker_manifests_without_steps_are_ignored(
    tmp_path: pathlib.Path,
) -> None:
    """Keep non-composite runtime metadata outside the uses contract."""
    workflow, actions = sources(
        tmp_path,
        {"jobs": {}},
        {"runs": {"using": "docker", "image": "docker://alpine:latest"}},
    )

    refs = list(action_references(workflow, actions))

    assert not refs, (
        "non-composite Docker runtime metadata is outside the uses contract"
    )


def test_directory_walk_errors_use_the_contract_error() -> None:
    """Translate inaccessible nested directories to the named shape error."""
    with pytest.raises(UnreadableWorkflowError):
        _raise_unreadable_directory(PermissionError("directory is unreadable"))


@pytest.mark.parametrize(
    ("workflow", "action", "error"),
    [
        (
            {"jobs": {"x": {"steps": "bad"}}},
            {"runs": {"using": "node20"}},
            NotAListError,
        ),
        (
            {"jobs": {"x": {"steps": ["bad"]}}},
            {"runs": {"using": "node20"}},
            NotAMappingError,
        ),
        (
            {"jobs": {"x": {"uses": 5}}},
            {"runs": {"using": "node20"}},
            UnsupportedUsesError,
        ),
        ({"jobs": {}}, {"runs": "bad"}, NotAMappingError),
        ({"jobs": {}}, {"runs": {"using": "composite"}}, NotAListError),
    ],
)
def test_malformed_shapes_fail(
    tmp_path: pathlib.Path, workflow: object, action: object, error: type[Exception]
) -> None:
    """Malformed containers must never disappear from the traversal."""
    workflow_source, action_source = sources(tmp_path, workflow, action)
    with pytest.raises(error):
        list(action_references(workflow_source, action_source))


def test_duplicate_keys_fail(tmp_path: pathlib.Path) -> None:
    """Duplicate YAML keys cannot conceal an unpinned reference."""
    workflow, actions = sources(tmp_path, {"jobs": {}}, {"runs": {"using": "node20"}})
    (workflow.workflow_dir / "ci.yml").write_text(
        "jobs: {}\njobs: {}\n", encoding="utf-8"
    )
    with pytest.raises(UnparsableWorkflowError):
        list(action_references(workflow, actions))


@pytest.mark.parametrize("name", REPOSITORY.names())
def test_repository_workflow_pins(name: str) -> None:
    """Every workflow reference satisfies the immutable reference policy."""
    for site, ref in action_references(REPOSITORY, ACTION_MANIFESTS):
        if site.startswith(f"{name}:"):
            validate_reference(site, ref)


@pytest.mark.parametrize("name", ACTION_MANIFESTS.names())
def test_repository_action_pins(name: str) -> None:
    """Every local manifest reference satisfies the immutable reference policy."""
    for site, ref in action_references(REPOSITORY, ACTION_MANIFESTS):
        if site.startswith(f"{name}:"):
            validate_reference(site, ref)


def test_privileged_workflows_and_composites_are_discovered() -> None:
    """Ensure the guarded estate cannot disappear silently."""
    refs = list(action_references(REPOSITORY, ACTION_MANIFESTS))
    for name in ("release.yml", "coverage-main.yml", "delayed-pr-comment.yml"):
        assert any(site.startswith(f"{name}:") for site, _ in refs), (
            f"{name} must contain references"
        )
    assert "pure-python-wheel/action.yml" in ACTION_MANIFESTS.names(), (
        "the release composite must be discovered"
    )


@pytest.mark.parametrize("place", ["job", "step", "composite"])
def test_mutable_refs_are_rejected_at_each_site(
    tmp_path: pathlib.Path, place: str
) -> None:
    """The policy applies equally to reusable workflows and action steps."""
    job: dict[str, object] = {"steps": [{"run": "true"}]}
    action: dict[str, object] = {"runs": {"using": "node20"}}
    match place:
        case "job":
            job = {"uses": "owner/repo/workflow.yml@main"}
        case "step":
            job = {"steps": [{"uses": "owner/repo@v1"}]}
        case _:
            action = {
                "runs": {"using": "composite", "steps": [{"uses": "owner/repo@v1"}]}
            }
    workflow, actions = sources(tmp_path, {"jobs": {"test": job}}, action)
    refs = list(action_references(workflow, actions))
    with pytest.raises(UnsupportedUsesError):
        validate_reference(
            *next((site, ref) for site, ref in refs if "owner/repo" in ref)
        )


def test_yaml_comment_and_digest_are_accepted(tmp_path: pathlib.Path) -> None:
    """YAML comments do not become part of the pinned ref; digests are immutable."""
    workflow, actions = sources(tmp_path, {"jobs": {}}, {"runs": {"using": "node20"}})
    (workflow.workflow_dir / "ci.yml").write_text(
        f"jobs:\n  test:\n    steps:\n      - uses: owner/repo@{'a' * 40} # v1.0.0\n",
        encoding="utf-8",
    )
    for site, ref in action_references(workflow, actions):
        validate_reference(site, ref)
    validate_reference("test", f"docker://image@sha256:{'f' * 64}")


def test_missing_ref_is_rejected() -> None:
    """External references must carry an explicit immutable revision."""
    with pytest.raises(UnsupportedUsesError):
        validate_reference("test", "owner/repo")


def test_extracted_readers_preserve_sites(tmp_path: pathlib.Path) -> None:
    """Each reader returns its own declaration and the pin classifier rejects tags."""
    workflow, actions = sources(
        tmp_path,
        {"jobs": {"x": {"uses": "owner/repo@main"}}},
        {"runs": {"using": "composite", "steps": [{"uses": "./local"}]}},
    )
    assert list(_workflow_references(workflow)) == [
        ("ci.yml:jobs.x", "owner/repo@main")
    ], "workflow site must be retained"
    assert list(_manifest_references(actions)) == [
        ("example/action.yml:runs.steps[0]", "./local")
    ], "manifest site must be retained"
    assert not _is_pinned("owner/repo@main"), "mutable tags must fail"
    assert _is_pinned("./local"), "local actions must pass"
