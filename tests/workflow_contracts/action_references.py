"""Read and validate every action and reusable workflow reference."""

from __future__ import annotations

import re
import typing as typ

if typ.TYPE_CHECKING:
    from collections import abc as cabc

from .workflow_support import (
    ActionManifestSource,
    NotAListError,
    NotAMappingError,
    UnsupportedUsesError,
    WorkflowSource,
    as_mapping,
)


def validate_reference(site: str, ref: object) -> None:
    """Require local references, digest-pinned images or full commit SHAs.

    Parameters
    ----------
    site : str
        Location of the reference.
    ref : object
        The declared reference.

    Raises
    ------
    UnsupportedUsesError
        If the reference is malformed or mutable.
    """
    if not isinstance(ref, str):
        raise UnsupportedUsesError(site, ref)
    if not _is_pinned(ref):
        raise UnsupportedUsesError(site, ref)


def _is_pinned(ref: str) -> bool:
    pattern = (
        r"(?:\./.+|docker://[^\s@]+@sha256:[0-9a-f]{64}"
        r"|[^/\s@]+/[^/\s@]+(?:/[^/\s@]+)*@[0-9a-f]{40})"
    )
    return bool(re.fullmatch(pattern, ref, flags=re.DOTALL))


def _uses(site: str, mapping: dict[str, object]) -> cabc.Iterator[tuple[str, str]]:
    if "uses" in mapping:
        ref = mapping["uses"]
        if not isinstance(ref, str):
            raise UnsupportedUsesError(site, ref)
        yield site, ref


def _steps(site: str, steps: object) -> cabc.Iterator[tuple[str, str]]:
    if not isinstance(steps, list):
        raise NotAListError(site)
    for index, value in enumerate(steps):
        where = f"{site}[{index}]"
        step = as_mapping(value)
        if step is None:
            raise NotAMappingError(where)
        yield from _uses(where, step)


def action_references(
    workflows: WorkflowSource, actions: ActionManifestSource
) -> cabc.Iterator[tuple[str, str]]:
    """Yield each declared reference with its file and declaration site.

    Parameters
    ----------
    workflows : WorkflowSource
        Source of workflow documents.
    actions : ActionManifestSource
        Source of action manifests.

    Yields
    ------
    tuple[str, str]
        Declaration site and reference.

    """
    yield from _workflow_references(workflows)
    yield from _manifest_references(actions)


def _workflow_references(workflows: WorkflowSource) -> cabc.Iterator[tuple[str, str]]:
    for name in workflows.names():
        for job_name, job in workflows.jobs(name).items():
            site = f"{name}:jobs.{job_name}"
            yield from _uses(site, job)
            if "steps" in job:
                yield from _steps(f"{site}.steps", job["steps"])


def _manifest_references(
    actions: ActionManifestSource,
) -> cabc.Iterator[tuple[str, str]]:
    for name in actions.names():
        runs = as_mapping(actions.document(name).get("runs"))
        if runs is None:
            raise NotAMappingError(f"{name}:runs")
        if runs.get("using") == "composite" or "steps" in runs:
            yield from _steps(f"{name}:runs.steps", runs.get("steps"))
