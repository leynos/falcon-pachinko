"""Whether each lane can actually be reached by the events it is placed for.

The placement rules rest on claims about events. The fork fallback is only
worth its complexity if a fork can reach that lane; the bare label on the
other lanes is only correct if no fork can reach those. Both claims live in
the `on:` block, not in the job, so a contract that reads only `runs-on`
asserts the consequence and never the premise. When the premise silently
changes, the consequence reads as deliberate.

Run with:

    uv run pytest tests/workflow_contracts
"""

from __future__ import annotations

import pytest

from .lane_triggers import ON_KEY, triggers
from .test_runner_placement import (
    FORK_FALLBACK_LANE,
    PUSH_ONLY_UBICLOUD_LANES,
)
from .workflow_support import REPOSITORY, as_mapping


def test_on_is_read_under_the_boolean_key() -> None:
    """Refuse the reading that makes every trigger contract vacuous.

    YAML 1.1 parses the bare word ``on`` as ``True``, so a workflow's trigger
    block is keyed under the boolean. A contract reading ``"on"`` gets None,
    treats the workflow as declaring no events, and passes. This names the
    hazard so a later simplification back to the string fails here first.
    """
    document = REPOSITORY.document("ci.yml")

    assert ON_KEY in document, "a workflow's trigger block is keyed under True"
    assert "on" not in document, (
        "nothing is keyed under the string 'on'; reading it would make every "
        "trigger contract pass on an empty mapping"
    )


def test_the_fork_fallback_lane_serves_pull_requests() -> None:
    """Prove the premise the fork fallback rests on.

    If this workflow stopped triggering on `pull_request`, no fork could
    reach the lane, the expression would always take its Ubicloud arm, and a
    reader would find a fork fallback guarding nothing.
    """
    workflow_name, _ = FORK_FALLBACK_LANE
    declared = triggers(REPOSITORY, workflow_name)

    assert "pull_request" in declared, (
        f"{workflow_name} must trigger on pull_request, or its lane's fork "
        f"fallback guards an event that cannot occur; got {sorted(declared)}"
    )


@pytest.mark.parametrize(
    "workflow_name", sorted({name for name, _ in PUSH_ONLY_UBICLOUD_LANES})
)
def test_bare_label_workflows_never_serve_a_pull_request(
    workflow_name: str,
) -> None:
    """Prove the premise the bare labels rest on.

    These lanes name `ubicloud-standard-2` outright, which is correct only
    while no fork can reach them. Adding a `pull_request` trigger here would
    leave a fork's pull request asking for a runner it can never obtain, and
    the job would queue rather than fail.
    """
    declared = triggers(REPOSITORY, workflow_name)

    reachable_by_a_fork = {"pull_request", "pull_request_target"}
    assert not reachable_by_a_fork & set(declared), (
        f"{workflow_name} names a bare Ubicloud label, so it must not trigger "
        f"on a pull request; it declares {sorted(declared)}"
    )


#: The event filter each push-triggered Ubicloud workflow is documented to
#: carry: the key under its `push` trigger, and the exact list of patterns.
#: Restated rather than derived, for the reason the ceiling table is.
PUSH_FILTERS = {
    "ci.yml": ("branches", ["main"]),
    "coverage-main.yml": ("branches", ["main"]),
    "release.yml": ("tags", ["v*.*.*"]),
}


@pytest.mark.parametrize("workflow_name", sorted(PUSH_FILTERS))
def test_each_push_trigger_carries_its_documented_filter(workflow_name: str) -> None:
    """Hold the filters the placement argument rests on.

    The contracts above ask only whether a pull request can reach a lane.
    That leaves the filters themselves unasserted, and they carry the rest of
    the argument: a `coverage-main.yml` that lost `branches: [main]` would
    upload coverage from every branch, and a `release.yml` whose tag filter
    widened to `*` would publish a wheel from any tag at all. Both stay green
    under every other rule here.
    """
    declared = triggers(REPOSITORY, workflow_name).get("push")
    push = as_mapping(declared)
    expected_key, expected_patterns = PUSH_FILTERS[workflow_name]

    assert push is not None, (
        f"{workflow_name} must filter its push trigger; it declares {declared!r}"
    )
    assert push.get(expected_key) == expected_patterns, (
        f"{workflow_name} is documented to push-trigger on {expected_key} "
        f"{expected_patterns}; it declares {push!r}"
    )


def test_the_filter_table_covers_every_push_triggered_ubicloud_workflow() -> None:
    """Refuse a filter table that has fallen behind the workflows.

    The table is named, so a workflow added to the migration and not to it
    would be filtered by nothing and checked by nothing.
    """
    ubicloud = {FORK_FALLBACK_LANE[0]} | {name for name, _ in PUSH_ONLY_UBICLOUD_LANES}

    assert set(PUSH_FILTERS) == ubicloud, (
        "every Ubicloud workflow's push filter must be asserted; the table "
        f"names {sorted(PUSH_FILTERS)}, the lanes are in {sorted(ubicloud)}"
    )
