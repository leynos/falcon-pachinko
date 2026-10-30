"""Narrow YAML readers for the CI workflow contract tests.

Every accessor here resolves a repository workflow by name and answers a
question about it. Reading and parsing are fallible, and every query reaches
the filesystem through the two readers here, ``read_workflow`` and
``read_source``. They translate an I/O or YAML failure into an
``AssertionError`` naming the file, so a contract over an unreadable workflow
fails as a contract, citing the file, rather than raising an unattributed
parser error from whichever query happened to touch it first.
"""

from __future__ import annotations

import typing as typ
from pathlib import Path

from tests.helpers import strict_yaml

__all__ = (
    "ROOT",
    "WORKFLOW_DIR",
    "job",
    "jobs",
    "parse_document",
    "read_source",
    "read_workflow",
    "steps",
    "workflow_document",
    "workflow_sources",
)

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW_DIR = ROOT / ".github" / "workflows"


def parse_document(source: str, workflow_name: str) -> dict[object, object]:
    """Parse workflow source text, without narrowing its top-level keys.

    Parameters
    ----------
    source : str
        The workflow's YAML text, as `read_source` returns it.
    workflow_name : str
        The name to cite in a diagnostic. Taking the text rather than the path
        lets a caller that already holds the source parse it without a second
        read.

    Returns
    -------
    dict[object, object]
        The parsed document, whose trigger key YAML 1.1 reads as ``True``.

    Raises
    ------
    TypeError
        If parsing does not produce a mapping.

    Notes
    -----
    Fails the contract when the text is not valid YAML, declares a mapping key
    twice, or does not parse to a mapping, naming the file in each case.

    Examples
    --------
    >>> parse_document("jobs: {}", "ci.yml")
    {'jobs': {}}

    """
    document = strict_yaml.load(source, workflow_name)
    if not isinstance(document, dict):
        message = f"{workflow_name} must parse to a mapping"
        raise TypeError(message)
    return typ.cast("dict[object, object]", document)


def read_source(path: Path) -> str:
    """Return one workflow's source text, naming the file if it cannot be read.

    Parameters
    ----------
    path : Path
        The workflow file to read.

    Returns
    -------
    str
        The file's UTF-8 text.

    Raises
    ------
    AssertionError
        If the file cannot be read, naming the file and the ``OSError``.

    Examples
    --------
    >>> "jobs:" in read_source(WORKFLOW_DIR / "ci.yml")
    True

    """
    try:
        return path.read_text(encoding="utf-8")
    except OSError as error:
        message = f"{path.name} could not be read: {error}"
        raise AssertionError(message) from error


def read_workflow(path: Path) -> dict[object, object]:
    """Parse one workflow file, without narrowing its top-level keys.

    Parameters
    ----------
    path : Path
        The workflow file to parse.

    Returns
    -------
    dict[object, object]
        The parsed document, whose trigger key YAML 1.1 reads as ``True``.

    Notes
    -----
    Fails the contract when the file cannot be read, is not valid YAML,
    declares a mapping key twice, or does not parse to a mapping, naming the
    file in each case.

    Examples
    --------
    >>> "jobs" in read_workflow(WORKFLOW_DIR / "ci.yml")
    True

    """
    return parse_document(read_source(path), path.name)


def workflow_document(workflow_name: str) -> dict[object, object]:
    """Parse one repository workflow, without narrowing its top-level keys."""
    return read_workflow(WORKFLOW_DIR / workflow_name)


def _mapping(value: object, message: str) -> dict[str, object]:
    """Narrow a parsed YAML value to a string-keyed mapping."""
    if not (isinstance(value, dict) and all(isinstance(key, str) for key in value)):
        raise AssertionError(message)
    return typ.cast("dict[str, object]", value)


def jobs(workflow_name: str) -> dict[str, object]:
    """Load the jobs mapping from one repository workflow."""
    return _mapping(
        workflow_document(workflow_name).get("jobs"),
        f"{workflow_name} must declare jobs",
    )


def job(workflow_name: str, job_name: str) -> dict[str, object]:
    """Return one named job from a repository workflow."""
    return _mapping(
        jobs(workflow_name).get(job_name),
        f"{workflow_name} must define {job_name}",
    )


def steps(workflow_name: str, job_name: str) -> list[dict[str, object]]:
    """Return the validated steps for one workflow job.

    A reusable-workflow call declares ``uses:`` where a step list would go, so
    it has no ``steps:`` key and yields none. A ``steps:`` key of the wrong
    shape is reported rather than read as "no steps", which would let every
    "no step does X" contract over it pass having read nothing.

    Parameters
    ----------
    workflow_name : str
        File name of the workflow under ``.github/workflows``.
    job_name : str
        Job whose steps are wanted.

    Returns
    -------
    list[dict[str, object]]
        One narrowed mapping per declared step, in order.

    Raises
    ------
    TypeError
        If the workflow job declares steps in an unsupported shape.

    """
    payload = job(workflow_name, job_name)
    declared = payload.get("steps")
    if declared is None and "steps" not in payload:
        return []
    where = f"{workflow_name}:{job_name}"
    if not isinstance(declared, list):
        message = f"{where} must declare steps as a list"
        raise TypeError(message)
    return [
        _mapping(step, f"{where} step {index} must be a mapping")
        for index, step in enumerate(typ.cast("list[object]", declared))
    ]


def workflow_sources(directory: Path = WORKFLOW_DIR) -> list[tuple[str, str]]:
    """Return every workflow's name and source text.

    Parameters
    ----------
    directory : Path
        The directory to sweep. It defaults to this repository's workflows; a
        caller testing the strict loader passes a temporary one.

    Returns
    -------
    list[tuple[str, str]]
        Each ``*.yml`` or ``*.yaml`` file's name and text, sorted by path.
        GitHub accepts both extensions, so a sweep that read one would leave
        the other unchecked. Every read goes through ``read_source``, so an
        unreadable file fails by name.

    Raises
    ------
    AssertionError
        If ``directory`` is not a readable directory or holds no workflow.
        ``Path.glob`` returns nothing for a missing directory, and an empty
        sweep would satisfy every "no workflow does X" contract vacuously.

    """
    if not directory.is_dir():
        message = f"{directory} is not a workflow directory"
        raise AssertionError(message)
    paths = sorted((*directory.glob("*.yml"), *directory.glob("*.yaml")))
    if not paths:
        message = f"{directory} holds no workflow"
        raise AssertionError(message)
    return [(path.name, read_source(path)) for path in paths]
