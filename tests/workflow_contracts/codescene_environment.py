"""Hold the ``codescene`` environment to the uploading job (CV-005).

The CodeScene token lives in the ``codescene`` environment, whose deployment
policy admits ``main`` alone. So every job that calls the uploader declares
that environment, no other job does, and no workflow a pull request can start
declares it in any job: a declaration there would let branch code ask for the
token.

Split from the contracts so the rule can be driven over workflows written for
each case, not only over the files it guards.
"""

from __future__ import annotations

import typing as typ

from .codescene_scan import UPLOAD_ACTION, invokes, pull_request_workflows

if typ.TYPE_CHECKING:
    from .workflow_support import WorkflowSource

#: The environment holding the CodeScene token.
ENVIRONMENT: typ.Final[str] = "codescene"
MISSING: typ.Final[str] = f"uploads but does not declare `environment: {ENVIRONMENT}`"
STRAY: typ.Final[str] = f"declares `{ENVIRONMENT}` but uploads nothing"
REACHABLE: typ.Final[str] = (
    f"can be started by a pull request and declares `{ENVIRONMENT}`"
)
NO_UPLOADER: typ.Final[str] = "no workflow job calls the CodeScene uploader"


def environment_name(job: dict[str, object]) -> str | None:
    """Return the environment a job declares, from either accepted form.

    Parameters
    ----------
    job : dict[str, object]
        One job mapping.

    Returns
    -------
    str | None
        The environment's name, or None when the job declares none.

    Examples
    --------
    >>> environment_name({"environment": "codescene"})
    'codescene'
    >>> environment_name({"environment": {"name": "codescene", "url": "x"}})
    'codescene'
    >>> environment_name({}) is None
    True
    """
    match job.get("environment"):
        case str() as name:
            return name
        case {"name": str() as name}:
            return name
        case _:
            return None


def _uploads(job: dict[str, object]) -> bool:
    """Return whether a job has a step calling the CodeScene uploader."""
    declared = job.get("steps")
    if not isinstance(declared, list):
        return False
    return any(
        isinstance(step, dict) and invokes(step, UPLOAD_ACTION) for step in declared
    )


def environment_violations(source: WorkflowSource) -> list[str]:
    """Report every departure from the ``codescene`` environment placement.

    Parameters
    ----------
    source : WorkflowSource
        Where to read.

    Returns
    -------
    list[str]
        One message per violation; empty when the placement holds. A source
        with no uploading job is itself a violation, so the rule cannot pass
        by finding nothing to check.
    """
    problems: list[str] = []
    uploaders = 0
    for name in source.names():
        for job_id, job in source.jobs(name).items():
            declares = environment_name(job) == ENVIRONMENT
            if _uploads(job):
                uploaders += 1
                if not declares:
                    problems.append(f"{name}:{job_id} {MISSING}")
            elif declares:
                problems.append(f"{name}:{job_id} {STRAY}")
    if uploaders == 0:
        problems.append(NO_UPLOADER)
    for name in pull_request_workflows(source):
        problems.extend(
            f"{name}:{job_id} {REACHABLE}"
            for job_id, job in source.jobs(name).items()
            if environment_name(job) == ENVIRONMENT
        )
    return problems
