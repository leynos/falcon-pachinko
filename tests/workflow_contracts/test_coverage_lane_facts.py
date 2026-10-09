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


def _is_shallow(depth: object) -> bool:
    """Report whether a ``fetch-depth`` value is a literal number of at least one.

    ``actions/checkout`` floors the number, so a value below one in any
    spelling (``0``, ``"00"``, ``0.0``, ``0.5``, a negative) fetches full
    history. So does one the reader cannot resolve, such as an expression or a
    boolean, because the contract cannot prove it shallow.

    Parameters
    ----------
    depth : object
        The ``fetch-depth`` value as the workflow document holds it.

    Returns
    -------
    bool
        ``True`` only for a literal number of at least one.
    """
    if isinstance(depth, bool):
        return False
    try:
        return float(str(depth)) >= 1
    except ValueError:
        return False


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
        if depth is not None and not _is_shallow(depth):
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


@pytest.mark.parametrize(
    "depth",
    [0, "0", "00", 0.0, 0.5, -1, "${{ inputs.depth }}"],
    ids=["integer", "string", "padded", "float", "fraction", "negative", "expression"],
)
def test_a_full_history_checkout_is_found(
    tmp_path: pathlib.Path, depth: object
) -> None:
    """Scenario: the lane's checkout asks for full history or a depth it cannot resolve.

    Invariant: the reader finds it, so the contract above fails on the case it
    exists for rather than passing over an empty reading.
    """
    source = source_over(tmp_path, {PR_WORKFLOW: _lane({"fetch-depth": depth})})

    assert full_history_checkouts(source) == [depth], (
        "a checkout requesting full history must be found"
    )


@pytest.mark.parametrize(
    "checkout_with",
    [None, {"persist-credentials": False}, {"fetch-depth": 1}, {"fetch-depth": "2"}],
    ids=["no inputs", "other inputs", "shallow depth", "string depth"],
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


@pytest.mark.parametrize(
    "lane",
    [{"jobs": {PR_JOB: {"steps": "not a list"}}}, {"jobs": {PR_JOB: {}}}],
    ids=["steps not a list", "no steps"],
)
def test_a_lane_without_readable_steps_is_refused(
    tmp_path: pathlib.Path, lane: dict[str, object]
) -> None:
    """Scenario: the lane's steps are missing or malformed.

    Invariant: the reader refuses rather than reporting a shallow lane.
    """
    source = source_over(tmp_path, {PR_WORKFLOW: lane})

    with pytest.raises(AssertionError, match="must declare its steps"):
        full_history_checkouts(source)


def test_a_checkout_with_malformed_inputs_is_not_found(tmp_path: pathlib.Path) -> None:
    """Scenario: the checkout's ``with`` is not a mapping.

    Invariant: it carries no ``fetch-depth``, so it is not reported.
    """
    source = source_over(tmp_path, {PR_WORKFLOW: _lane(["fetch-depth: 0"])})

    assert not full_history_checkouts(source), "a malformed ``with`` carries no depth"
