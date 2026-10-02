"""Facts about this repository's coverage lane that the shared library leaves out.

The CV-005 contract library (``cv005-contracts check``, run by
``make check-cv005``) holds the coverage and CodeScene shape every repository
shares. These contracts keep what is this repository's own: the
pull-request lane checks out a shallow clone. ``generate-coverage`` compares
a measured percentage with a stored baseline and never runs git, so full
history buys nothing and a ``fetch-depth: 0`` left over from an older check
step only slows the lane.

Run with:

    uv run pytest tests/workflow_contracts
"""

from __future__ import annotations

import typing as typ

import pytest

from .test_label_reading import source_over
from .workflow_support import REPOSITORY, WorkflowSource

if typ.TYPE_CHECKING:
    import pathlib

PR_WORKFLOW: typ.Final[str] = "ci.yml"
PR_JOB: typ.Final[str] = "lint-test"
CHECKOUT_ACTION: typ.Final[str] = "actions/checkout"


def full_history_checkouts(source: WorkflowSource) -> list[object]:
    """Return the ``fetch-depth`` of each checkout in the lane that is not shallow.

    Parameters
    ----------
    source : WorkflowSource
        Where to read the lane from.

    Returns
    -------
    list[object]
        One ``fetch-depth`` value per checkout step asking for full history.
    """
    job = source.jobs(PR_WORKFLOW)[PR_JOB]
    steps = job.get("steps")
    assert isinstance(steps, list), f"{PR_WORKFLOW}:{PR_JOB} must declare its steps"
    found: list[object] = []
    for step in steps:
        if not isinstance(step, dict) or CHECKOUT_ACTION not in str(step.get("uses")):
            continue
        inputs = step.get("with")
        depth = inputs.get("fetch-depth") if isinstance(inputs, dict) else None
        if depth is not None and str(depth) == "0":
            found.append(depth)
    return found


def _lane(checkout_with: object) -> dict[str, object]:
    """Build a one-job pull-request workflow whose checkout carries *checkout_with*."""
    step: dict[str, object] = {"uses": f"{CHECKOUT_ACTION}@v7"}
    if checkout_with is not None:
        step["with"] = checkout_with
    return {"jobs": {PR_JOB: {"steps": [step]}}}


def test_the_pull_request_lane_fetches_no_history() -> None:
    """Keep the coverage lane's checkout shallow."""
    assert not full_history_checkouts(REPOSITORY), (
        f"{PR_WORKFLOW}:{PR_JOB} fetches full history; the ratchet reads no "
        f"commits, so the default shallow clone serves it"
    )


@pytest.mark.parametrize("depth", [0, "0"], ids=["integer", "string"])
def test_a_full_history_checkout_is_found(
    tmp_path: pathlib.Path, depth: object
) -> None:
    """Scenario: the lane's checkout asks for ``fetch-depth: 0`` in either form.

    Invariant: the reader finds it, so the contract above fails on the case it
    exists for rather than passing over an empty reading.
    """
    source = source_over(tmp_path, {PR_WORKFLOW: _lane({"fetch-depth": depth})})

    assert full_history_checkouts(source) == [depth], (
        "a checkout requesting full history must be found"
    )


@pytest.mark.parametrize(
    "checkout_with",
    [None, {"persist-credentials": False}, {"fetch-depth": 1}],
    ids=["no inputs", "other inputs", "shallow depth"],
)
def test_a_shallow_checkout_is_not_found(
    tmp_path: pathlib.Path, checkout_with: object
) -> None:
    """Scenario: the lane's checkout keeps the default or an explicit shallow depth.

    Invariant: none of these is reported, so the contract does not refuse the
    shape it must accept.
    """
    source = source_over(tmp_path, {PR_WORKFLOW: _lane(checkout_with)})

    assert not full_history_checkouts(source), "a shallow checkout must not be reported"
