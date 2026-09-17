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

from .test_runner_placement import (
    FORK_FALLBACK_LANE,
    PUSH_ONLY_UBICLOUD_LANES,
)
from .workflow_support import NotAMappingError, as_mapping, workflow

#: `on` is the YAML 1.1 boolean `True`, so a parsed workflow keys its trigger
#: block under the boolean and not the string. Reading `"on"` returns None
#: and every trigger contract then passes on an empty mapping.
ON_KEY = True


def triggers(workflow_name: str) -> dict[str, object]:
    """Return a workflow's trigger block.

    Parameters
    ----------
    workflow_name : str
        The workflow file name.

    Returns
    -------
    dict[str, object]
        The events the workflow declares, mapped to their filters.

    Raises
    ------
    NotAMappingError
        If the workflow declares no trigger mapping.
    """
    declared = as_mapping(workflow(workflow_name).get(ON_KEY))
    if declared is None:
        raise NotAMappingError(f"{workflow_name}:on")
    return declared


def test_on_is_read_under_the_boolean_key() -> None:
    """Refuse the reading that makes every trigger contract vacuous.

    YAML 1.1 parses the bare word ``on`` as ``True``, so a workflow's trigger
    block is keyed under the boolean. A contract reading ``"on"`` gets None,
    treats the workflow as declaring no events, and passes. This names the
    hazard so a later simplification back to the string fails here first.
    """
    document = workflow("ci.yml")

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
    declared = triggers(workflow_name)

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
    declared = triggers(workflow_name)

    reachable_by_a_fork = {"pull_request", "pull_request_target"}
    assert not reachable_by_a_fork & set(declared), (
        f"{workflow_name} names a bare Ubicloud label, so it must not trigger "
        f"on a pull request; it declares {sorted(declared)}"
    )
