"""CV-005: the shape of the one workflow that publishes to CodeScene.

The boundary contracts clear everything a pull request can run. These hold the
other side: the publisher's triggers, the conditions its upload runs under,
whether two trunk generations can overlap, the action pins, and whether the
trunk measures what the pull-request ratchet compares against. Each is a way
the estate rule fails while every pull-request lane stays clean.

Run with:

    uv run pytest tests/workflow_contracts
"""

from __future__ import annotations

import pytest

from .codescene_scan import (
    FULL_SHA,
    PR_JOB,
    PR_WORKFLOW,
    PUBLISHER,
    PUBLISHER_JOB,
    SELECTION_INPUTS,
    _walk,
    coverage_inputs,
    declared_steps,
    steps,
    triggers,
)
from .guard_conditions import conjuncts
from .pull_request_reach import PULL_REQUEST_EVENTS
from .workflow_support import REPOSITORY

MAIN_REF = "github.ref == 'refs/heads/main'"


def test_the_upload_is_restricted_to_the_main_ref() -> None:
    """Publish only from `main`, whatever the event selected.

    `workflow_dispatch` can select any branch or tag. The push trigger's
    `branches: [main]` says nothing about a dispatch, so without a ref guard on
    the step itself a dispatch from a feature branch publishes that branch's
    coverage through the main-owned upload, and CodeScene records it as the
    trunk's.

    The guard is read as a conjunction, not searched for a phrase: a substring
    check passes `... && github.ref == 'refs/heads/main' || github.event_name
    == 'workflow_dispatch'`, which makes every conjunct optional, and
    :func:`guard_conditions.conjuncts` refuses the `||` outright.
    """
    uploads = [
        step
        for step in steps(REPOSITORY, PUBLISHER, PUBLISHER_JOB)
        if "upload-codescene-coverage@" in str(step.get("uses", ""))
    ]

    assert uploads, f"{PUBLISHER} must upload the trunk report"
    for step in uploads:
        found = conjuncts(str(step.get("if", "")))
        assert MAIN_REF in found, (
            f"{PUBLISHER}'s upload must carry {MAIN_REF!r} as a conjunct; it has "
            f"{found}"
        )


def test_the_publisher_serializes_its_trunk_generations() -> None:
    """Let one trunk generation finish before the next starts.

    The shared action saves a fresh ratchet-baseline cache per successful push
    and later runs restore the newest match. Two overlapping pushes to `main`
    would both publish, and the older commit finishing last would leave its
    baseline as the one every pull request is then measured against. Nothing
    is cancelled: a trunk generation that has started is the one that should
    finish.
    """
    declared = REPOSITORY.document(PUBLISHER).get("concurrency")

    assert isinstance(declared, dict), (
        f"{PUBLISHER} must declare a concurrency group; it declares {declared!r}"
    )
    assert declared.get("cancel-in-progress") is False, (
        f"{PUBLISHER} must not cancel a running trunk generation; it declares "
        f"cancel-in-progress={declared.get('cancel-in-progress')!r}"
    )
    assert "github.ref" in str(declared.get("group")), (
        f"{PUBLISHER}'s group must serialize per ref so two pushes to main "
        f"queue; it declares {declared.get('group')!r}"
    )


def test_the_publisher_is_not_reachable_from_a_pull_request() -> None:
    """Keep the workflow that holds the credential off pull-request events."""
    declared = set(triggers(REPOSITORY, PUBLISHER))

    assert not PULL_REQUEST_EVENTS & declared, (
        f"{PUBLISHER} holds the CodeScene credential, so it must not trigger "
        f"on a pull request; it declares {sorted(declared)}"
    )
    assert declared <= {"push", "workflow_dispatch"}, (
        f"{PUBLISHER} may trigger only on a push or a dispatch; it declares "
        f"{sorted(declared)}"
    )
    push = triggers(REPOSITORY, PUBLISHER)["push"]
    assert isinstance(push, dict), f"{PUBLISHER} must filter its push trigger"
    assert push.get("branches") == ["main"], (
        f"{PUBLISHER} must be restricted to pushes to main; got "
        f"{push.get('branches')!r}"
    )


def test_the_publisher_uploads_rather_than_checks() -> None:
    """Upload the trunk report; never gate on it from here.

    ``mode: check`` is the pull-request form, and the form that failed.
    """
    uploads = [
        step
        for step in steps(REPOSITORY, PUBLISHER, PUBLISHER_JOB)
        if "upload-codescene-coverage@" in str(step.get("uses", ""))
    ]

    assert uploads, f"{PUBLISHER} must upload the trunk report to CodeScene"
    for step in uploads:
        inputs = step.get("with")
        assert isinstance(inputs, dict), f"{PUBLISHER} upload must declare inputs"
        assert inputs.get("mode") == "upload", (
            f"{PUBLISHER} must upload, not {inputs.get('mode')!r}"
        )


def test_no_caller_passes_the_deprecated_installer_checksum() -> None:
    """Refuse the input the shared action now rejects.

    From shared-actions f68e8e2e the CodeScene CLI is pinned through a manifest
    and a non-empty ``installer-checksum`` fails the run outright.
    ``archive-checksum`` replaces it, so passing the old input is a red lane
    rather than a deprecation warning.
    """
    offending = [
        f"{name}: {path}"
        for name in REPOSITORY.names()
        for path, text in _walk(REPOSITORY.document(name), name)
        if path.endswith(".installer-checksum") and text
    ]

    assert not offending, (
        f"installer-checksum is rejected when non-empty; use archive-checksum: "
        f"{offending}"
    )


def test_every_shared_coverage_action_is_sha_pinned_to_one_revision() -> None:
    """Pin both shared actions to one full commit SHA.

    Two revisions would let the publisher be repinned by itself, and the
    baseline it wrote would then come from a different implementation than the
    one measuring the pull request compared against it.
    """
    used = {
        str(step.get("uses"))
        for name in REPOSITORY.names()
        for step in declared_steps(REPOSITORY, name)
        if "generate-coverage@" in str(step.get("uses", ""))
        or "upload-codescene-coverage@" in str(step.get("uses", ""))
    }
    revisions = {reference.rsplit("@", 1)[1] for reference in used}

    assert used, "the repository must invoke the shared coverage actions"
    unpinned = [
        reference
        for reference in sorted(used)
        if not FULL_SHA.fullmatch(reference.rsplit("@", 1)[1])
    ]
    assert not unpinned, (
        f"every shared coverage action must be pinned to a full lowercase "
        f"40-character SHA; found {unpinned}"
    )
    assert len(revisions) == 1, (
        f"both shared coverage actions must use one revision; found {revisions}"
    )


def test_the_pull_request_lane_declines_publication() -> None:
    """Keep report publication to the workflow that owns it.

    The action archives the report under a step of its own, which no scanner
    over this workflow's steps can see, so declining the archive explicitly is
    the only way the boundary is observable here.
    """
    inputs = coverage_inputs(REPOSITORY, PR_WORKFLOW, PR_JOB)

    assert inputs.get("publish-artefact") == "false", (
        f"{PR_WORKFLOW}:{PR_JOB} must decline the artefact; publication belongs "
        f"to {PUBLISHER}. It declares {inputs.get('publish-artefact')!r}"
    )


def test_the_publisher_publishes_the_report() -> None:
    """Leave the publisher on the action's default.

    Setting ``publish-artefact`` here as the pull-request lane does would leave
    the upload with no report to read.
    """
    inputs = coverage_inputs(REPOSITORY, PUBLISHER, PUBLISHER_JOB)

    assert "publish-artefact" not in inputs, (
        f"{PUBLISHER} must keep the action's default; it declares "
        f"publish-artefact={inputs.get('publish-artefact')!r}"
    )


@pytest.mark.parametrize("field", SELECTION_INPUTS)
def test_both_lanes_measure_the_same_thing(field: str) -> None:
    """Hold the trunk generation and the pull-request check to one selection.

    The ratchet compares a pull request's changed-line coverage against the
    baseline the trunk wrote. If the two lanes select differently, that
    comparison is between two different measurements and a pull request can
    fail or pass on the difference rather than on its own change.
    """
    gate = coverage_inputs(REPOSITORY, PR_WORKFLOW, PR_JOB)
    publisher = coverage_inputs(REPOSITORY, PUBLISHER, PUBLISHER_JOB)

    assert gate.get(field) == publisher.get(field), (
        f"{PR_WORKFLOW} selects {field}={gate.get(field)!r} and {PUBLISHER} "
        f"selects {publisher.get(field)!r}; the baseline would not be "
        "comparable with what the ratchet checks"
    )


def test_the_upload_reads_the_format_that_was_generated() -> None:
    """Upload the report in the format the generator wrote.

    The two are separate inputs on separate steps, so nothing else notices
    them disagreeing. CodeScene would be handed a cobertura document labelled
    as lcov, or the reverse, and the upload fails at parse time on the trunk
    rather than anywhere a pull request could see.
    """
    generated = coverage_inputs(REPOSITORY, PUBLISHER, PUBLISHER_JOB).get("format")
    uploads = [
        step
        for step in steps(REPOSITORY, PUBLISHER, PUBLISHER_JOB)
        if "upload-codescene-coverage@" in str(step.get("uses", ""))
    ]

    assert uploads, f"{PUBLISHER} must upload the trunk report"
    for step in uploads:
        inputs = step.get("with")
        assert isinstance(inputs, dict), f"{PUBLISHER} upload must declare inputs"
        assert inputs.get("format") == generated, (
            f"{PUBLISHER} generates {generated!r} and uploads {inputs.get('format')!r}"
        )


def test_the_selection_list_covers_every_input_both_lanes_declare() -> None:
    """Refuse a selection list that has fallen behind the workflows.

    The list is named, so an input added to both lanes and not to it would
    simply not be compared, and the two could then drift on it.
    """
    gate = set(coverage_inputs(REPOSITORY, PR_WORKFLOW, PR_JOB))
    publisher = set(coverage_inputs(REPOSITORY, PUBLISHER, PUBLISHER_JOB))
    shared = gate & publisher

    assert shared == set(SELECTION_INPUTS), (
        "every input both lanes declare must be compared; unlisted: "
        f"{sorted(shared - set(SELECTION_INPUTS))}, listed but absent: "
        f"{sorted(set(SELECTION_INPUTS) - shared)}"
    )
