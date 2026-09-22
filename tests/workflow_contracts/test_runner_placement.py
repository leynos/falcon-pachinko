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

import re

import pytest

from .runner_labels import (
    collapse_label_whitespace,
    declared_labels,
    job_labels,
    labels_in_use,
    runner_expression,
)
from .workflow_support import (
    FORK_FIELD,
    GITHUB_HOSTED_LABELS,
    GITHUB_HOSTED_LINUX,
    REPOSITORY,
    ROOT,
    UBICLOUD_LINUX_LABEL,
)

#: The lane that serves pull requests, and so the only one that can be
#: reached by a fork.
FORK_FALLBACK_LANE = ("ci.yml", "lint-test")
#: Each Ubicloud lane's ceiling in minutes, as the developer guide's table
#: publishes it. The values are restated here rather than derived, because
#: the contract's job is to hold the guide and the workflow to one another:
#: a ceiling widened in the workflow alone leaves the guide describing a lane
#: that no longer exists, and the guide is what the next re-sizing reads.
LANE_CEILINGS = {
    ("ci.yml", "lint-test"): 15,
    ("coverage-main.yml", "coverage-upload"): 15,
    ("release.yml", "pure-wheel"): 30,
    ("release.yml", "release"): 30,
}
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
    ("build-wheels.yml", "build"): "workflow_call, and Windows and macOS legs",
}


def _job(workflow_name: str, job_name: str) -> dict[str, object]:
    """Return one job document, failing with its location if it is absent.

    This module is where the repository's source is composed; the readers
    it calls take their source explicitly.

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
    declared = REPOSITORY.jobs(workflow_name)
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
    found = REPOSITORY.names()
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
        for reference in declared_labels(REPOSITORY)
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
    job_document = _job(workflow_name, job_name)
    reason = GITHUB_HOSTED_JOBS[workflow_name, job_name]
    resolved = job_labels(workflow_name, job_name, job_document)

    assert resolved, (
        f"{workflow_name}:{job_name} must resolve to at least one label, or "
        "this contract asserts nothing about it"
    )
    elsewhere = resolved - GITHUB_HOSTED_LABELS
    assert not elsewhere, (
        f"{workflow_name}:{job_name} stays GitHub-hosted ({reason}); it can "
        f"run on {sorted(elsewhere)}, which GitHub does not host"
    )


def test_every_ubicloud_lane_declares_a_ceiling() -> None:
    """Bound every lane that bills by the minute.

    An Ubicloud runner registers as a just-in-time self-hosted runner, so
    GitHub's six-hour cap does not apply to it. A lane without
    `timeout-minutes` can therefore run until the five-day limit, and the
    absence is invisible until the first hung job.
    """
    unbounded = []
    for workflow_name in REPOSITORY.names():
        for job_name, job_document in REPOSITORY.jobs(workflow_name).items():
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
        for workflow_name in REPOSITORY.names()
        for job_name, job_document in REPOSITORY.jobs(workflow_name).items()
        if isinstance(job_document.get("runs-on"), str)
        and UBICLOUD_LINUX_LABEL
        in collapse_label_whitespace(str(job_document.get("runs-on")))
    }
    expected = {FORK_FALLBACK_LANE, *PUSH_ONLY_UBICLOUD_LANES}

    assert visited == expected, (
        "the ceiling contract must visit exactly the Ubicloud lanes; "
        f"visited {sorted(visited)}, expected {sorted(expected)}"
    )


#: Every workflow that declares a runner label anywhere. `build-wheels.yml`
#: is in this set deliberately: it is `workflow_call` and nothing in this
#: repository calls it today, and it is still asked the registry question.
LABEL_DECLARING_WORKFLOWS = frozenset(
    {
        "build-wheels.yml",
        "ci.yml",
        "coverage-main.yml",
        "delayed-pr-comment.yml",
        "release.yml",
    }
)


def test_the_registry_reads_every_workflow_that_declares_a_label() -> None:
    """Account for a callerless `workflow_call` file rather than exempting it.

    `build-wheels.yml` declares six labels and nothing calls it. The registry
    could either account for its labels or exclude callerless
    `workflow_call` files by rule; this repository accounts for them. A file
    is one line away from gaining a caller, and "callerless" is not even a
    local property, since a caller may live in another repository, so a rule
    that skipped the file would quietly stop asking the registry question for
    a workflow that could run tomorrow.

    The set is named rather than derived, so dropping a workflow from the
    traversal fails here instead of silently shrinking what the registry is
    held to.
    """
    reached = {reference.workflow for reference in declared_labels(REPOSITORY)}

    assert reached == LABEL_DECLARING_WORKFLOWS, (
        "the label traversal must reach every workflow that declares one, "
        "including callerless workflow_call files; reached "
        f"{sorted(reached)}"
    )


def test_the_registry_matches_the_labels_in_use() -> None:
    """Hold equality in both directions between registry and use.

    A subset assertion misses a registration that outlives its last use, and
    reads as a provider still in play. Filtering the labels in use by a
    provider prefix would silently exempt a second paid provider's labels,
    which is the substantive question the registry exists to ask, so the
    exemption is a named set of GitHub-hosted labels instead.
    """
    registered = REPOSITORY.registered_labels()

    assert set(registered) == labels_in_use(REPOSITORY), (
        "every label GitHub does not host must be registered, and every "
        f"registration must still be used; registered {sorted(registered)}, "
        f"in use {sorted(labels_in_use(REPOSITORY))}"
    )


@pytest.mark.parametrize(("workflow_name", "job_name"), sorted(LANE_CEILINGS))
def test_each_lane_declares_its_documented_ceiling(
    workflow_name: str, job_name: str
) -> None:
    """Hold each ceiling to the figure the developer guide publishes.

    The contract above refuses a lane with no ceiling at all. It says nothing
    about the number, so a ceiling widened from fifteen minutes to six hours
    would pass it while making the lane's bound meaningless and the guide's
    table wrong.
    """
    declared = _job(workflow_name, job_name).get("timeout-minutes")
    expected = LANE_CEILINGS[workflow_name, job_name]

    assert declared == expected, (
        f"{workflow_name}:{job_name} is documented at {expected} minutes; it "
        f"declares {declared!r}. Change the developer guide's table in the "
        "same commit, or the next re-sizing reads a figure that is not in "
        "force."
    )


def test_the_ceiling_table_covers_every_ubicloud_lane() -> None:
    """Refuse a ceiling table that has fallen behind the lanes.

    The table above is named rather than derived, which is what lets it hold
    the guide. A lane added to the migration and not to the table would
    simply not be checked, so the two sets are compared.
    """
    assert set(LANE_CEILINGS) == {FORK_FALLBACK_LANE, *PUSH_ONLY_UBICLOUD_LANES}, (
        "every Ubicloud lane must carry a documented ceiling; the table names "
        f"{sorted(LANE_CEILINGS)}"
    )


#: Paths named in prose, as ``path`` or ``path::test_name``.
_NAMED_TEST_PATH = re.compile(r"tests/[\w./-]+\.py(?:::(?P<test>\w+))?")


def test_the_registry_comment_names_a_contract_that_exists() -> None:
    """Keep the registry's comment pointing at the rule that holds it.

    `.github/actionlint.yaml` tells its next reader which contract enforces
    the equality it declares. That is the only signpost from the registry to
    the rule, and nothing else would notice it going stale: a renamed or
    merged contract file leaves the comment naming a path that is not there,
    and the reader concludes the equality is unenforced.
    """
    text = REPOSITORY.text(REPOSITORY.actionlint_config)
    named = list(_NAMED_TEST_PATH.finditer(text))

    assert named, (
        ".github/actionlint.yaml must name the contract that holds its "
        "registry to the labels in use"
    )
    for match in named:
        path = ROOT / match.group(0).split("::")[0]
        # Read through the source's own boundary, so a missing, unreadable or
        # undecodable file fails as a named contract error rather than a raw
        # `OSError` or `UnicodeDecodeError`.
        body = REPOSITORY.text(path)
        test_name = match.group("test")
        if test_name is not None:
            assert f"def {test_name}(" in body, (
                f".github/actionlint.yaml names {match.group(0)}, but "
                f"{path.name} defines no {test_name}"
            )
