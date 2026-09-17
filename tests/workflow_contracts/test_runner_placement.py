"""Where each job runs, and what happens when it does not finish.

Repository-owned Linux lanes run on Ubicloud. A pull request from a fork
cannot obtain an Ubicloud runner, so the one lane that serves pull requests
resolves its label from the head repository instead of naming one. Three
failures follow from that arrangement and none of them shows up in a green
run:

* a continuation indented one level deeper than its folded scalar keeps its
  line break, so the label carries a newline inside the expression and GitHub
  evaluates it regardless;
* a lane that moves to a per-minute runner without a ceiling inherits
  GitHub's six-hour default, which self-hosted just-in-time runners do not
  bound at all; and
* a label GitHub does not host is an unknown label to `actionlint` until it
  is registered, and a registration that outlives its last use reads as a
  provider still in play.

Run with:

    uv run pytest tests/workflow_contracts
"""

from __future__ import annotations

import pytest
import yaml

from .runner_labels import (
    collapse_label_whitespace,
    declared_labels,
    labels_in_use,
    runner_expression,
)
from .workflow_support import (
    ACTIONLINT_CONFIG,
    FORK_FIELD,
    GITHUB_HOSTED_LINUX,
    UBICLOUD_LINUX_LABEL,
    jobs,
    workflow_names,
)

#: The lane that serves pull requests, and so the only one that can be
#: reached by a fork.
FORK_FALLBACK_LANE = ("ci.yml", "lint-test")
#: Lanes that run on Ubicloud from a push or a tag only. A fork cannot reach
#: them, so they name their label outright; a guard here would be noise that
#: reads as protection.
PUSH_ONLY_UBICLOUD_LANES = (
    ("coverage-main.yml", "coverage-upload"),
    ("release.yml", "pure-wheel"),
    ("release.yml", "release"),
)
#: Jobs that stay GitHub-hosted, with the reason each one stays. Scheduled,
#: dispatch and `pull_request_target` work is not what this migration
#: targets, and public-repository minutes are free there.
GITHUB_HOSTED_JOBS = {
    ("delayed-pr-comment.yml", "delay_and_comment"): "workflow_dispatch only",
    ("get-codescene-sha.yml", "refresh-sha"): "workflow_dispatch only",
    ("build-wheels.yml", "build"): "workflow_call, and Windows and macOS legs",
}


def _job(workflow_name: str, job_name: str) -> dict[str, object]:
    """Return one job document, failing with its location if it is absent.

    Parameters
    ----------
    workflow_name : str
        The workflow file name.
    job_name : str
        The job key.

    Returns
    -------
    dict[str, object]
        The job mapping.
    """
    declared = jobs(workflow_name)
    assert job_name in declared, (
        f"{workflow_name} must declare a {job_name!r} job; it declares "
        f"{sorted(declared)}"
    )
    return declared[job_name]


def test_the_traversal_finds_every_workflow() -> None:
    """Refuse a vacuous pass.

    Every contract below walks the workflow directory. A traversal that found
    nothing would report success while asserting nothing, and the placement
    contracts are exactly the kind that would then stay green through a
    rename.
    """
    found = workflow_names()
    assert len(found) >= len(GITHUB_HOSTED_JOBS) + 1, (
        f"the traversal must reach this repository's workflows; found {found}"
    )


def test_no_declared_label_spans_lines() -> None:
    """Refuse a label a folded scalar broke across lines.

    A more-indented continuation puts a literal newline inside the
    expression. The document still parses, `actionlint` still passes and
    GitHub still evaluates the expression, so only the raw text says the
    declaration is wrong.
    """
    broken = [
        f"{reference.where} declares {reference.raw!r}"
        for reference in declared_labels()
        if "\n" in reference.raw
    ]
    assert not broken, (
        "a runner label must parse to a single line; a continuation indented "
        "deeper than its folded scalar keeps its line break and puts a "
        f"newline inside the expression: {broken}"
    )


def test_the_pull_request_lane_falls_back_for_forks() -> None:
    """Send a fork's pull request to GitHub-hosted Linux, everything else to Ubicloud.

    The guard is asserted as the whole field path. A sibling field of the same
    object, such as the head repository's ``private`` flag, reads almost
    identically and would route every fork pull request to a runner it can
    never obtain.
    """
    workflow_name, job_name = FORK_FALLBACK_LANE
    declared = _job(workflow_name, job_name).get("runs-on")
    assert isinstance(declared, str), (
        f"{workflow_name}:{job_name} must declare a runs-on label"
    )
    label = runner_expression(declared)

    assert label.guard == FORK_FIELD, (
        f"the lane must branch on {FORK_FIELD}, the only field that says a "
        f"runner cannot be obtained; got {label.guard!r}"
    )
    assert label.when_true == GITHUB_HOSTED_LINUX, (
        f"a fork pull request must fall back to {GITHUB_HOSTED_LINUX}; got "
        f"{label.when_true!r}"
    )
    assert label.when_false == UBICLOUD_LINUX_LABEL, (
        f"every other event must reach {UBICLOUD_LINUX_LABEL}; got {label.when_false!r}"
    )


@pytest.mark.parametrize(("workflow_name", "job_name"), PUSH_ONLY_UBICLOUD_LANES)
def test_push_only_lanes_name_the_label_outright(
    workflow_name: str, job_name: str
) -> None:
    """Keep a guard off a lane no fork can reach.

    These lanes trigger on a push or a tag. `github.event.pull_request` is
    null there, so a fork fallback would always take its second arm: a guard
    that can never hold reads as protection and is not.
    """
    declared = _job(workflow_name, job_name).get("runs-on")

    assert declared == UBICLOUD_LINUX_LABEL, (
        f"{workflow_name}:{job_name} triggers on push or tag only, so it must "
        f"name {UBICLOUD_LINUX_LABEL} outright; got {declared!r}"
    )


@pytest.mark.parametrize(("workflow_name", "job_name"), sorted(GITHUB_HOSTED_JOBS))
def test_named_jobs_stay_github_hosted(workflow_name: str, job_name: str) -> None:
    """Hold the placement rule's other half.

    A contract that only checked which jobs moved would stay green if a
    dispatch-only or `workflow_call` job were moved onto a paid runner too,
    which is the mistake the placement rule exists to prevent.
    """
    declared = _job(workflow_name, job_name).get("runs-on")
    reason = GITHUB_HOSTED_JOBS[workflow_name, job_name]
    resolved = (
        collapse_label_whitespace(declared) if isinstance(declared, str) else declared
    )

    assert UBICLOUD_LINUX_LABEL not in str(resolved), (
        f"{workflow_name}:{job_name} stays GitHub-hosted ({reason}); it "
        f"declares {resolved!r}"
    )


def test_every_ubicloud_lane_declares_a_ceiling() -> None:
    """Bound every lane that bills by the minute.

    An Ubicloud runner registers as a just-in-time self-hosted runner, so
    GitHub's six-hour cap does not apply to it. A lane without
    `timeout-minutes` can therefore run until the five-day limit, and the
    absence is invisible until the first hung job.
    """
    unbounded = []
    for workflow_name in workflow_names():
        for job_name, job_document in jobs(workflow_name).items():
            declared = job_document.get("runs-on")
            if not isinstance(declared, str):
                continue
            if UBICLOUD_LINUX_LABEL not in collapse_label_whitespace(declared):
                continue
            if not isinstance(job_document.get("timeout-minutes"), int):
                unbounded.append(f"{workflow_name}:{job_name}")
    assert not unbounded, (
        "every lane that can reach Ubicloud must declare timeout-minutes; "
        f"self-hosted runners carry no six-hour cap: {unbounded}"
    )


def test_the_ceiling_contract_sees_the_lanes_it_guards() -> None:
    """Refuse a ceiling contract that walks past every lane.

    The contract above is a loop with three `continue` branches. If the label
    test stopped matching, it would find nothing unbounded and pass while
    checking nothing, so the lanes it must have visited are named here.
    """
    visited = {
        (workflow_name, job_name)
        for workflow_name in workflow_names()
        for job_name, job_document in jobs(workflow_name).items()
        if isinstance(job_document.get("runs-on"), str)
        and UBICLOUD_LINUX_LABEL
        in collapse_label_whitespace(str(job_document.get("runs-on")))
    }
    expected = {FORK_FALLBACK_LANE, *PUSH_ONLY_UBICLOUD_LANES}

    assert visited == expected, (
        "the ceiling contract must visit exactly the Ubicloud lanes; "
        f"visited {sorted(visited)}, expected {sorted(expected)}"
    )


def test_the_registry_matches_the_labels_in_use() -> None:
    """Hold equality in both directions between registry and use.

    A subset assertion misses a registration that outlives its last use, and
    reads as a provider still in play. Filtering the labels in use by a
    provider prefix would silently exempt a second paid provider's labels,
    which is the substantive question the registry exists to ask, so the
    exemption is a named set of GitHub-hosted labels instead.
    """
    document = yaml.safe_load(ACTIONLINT_CONFIG.read_text(encoding="utf-8"))
    assert isinstance(document, dict), ".github/actionlint.yaml must be a mapping"
    section = document.get("self-hosted-runner")
    assert isinstance(section, dict), (
        ".github/actionlint.yaml must declare self-hosted-runner"
    )
    registered = section.get("labels")
    assert isinstance(registered, list), "self-hosted-runner must declare a labels list"

    assert set(registered) == labels_in_use(), (
        "every label GitHub does not host must be registered, and every "
        f"registration must still be used; registered {sorted(registered)}, "
        f"in use {sorted(labels_in_use())}"
    )
