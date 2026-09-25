"""Reading the runner labels a workflow declares.

A label is either a literal such as ``ubuntu-latest`` or a conditional
expression that resolves one at job setup. The expression form brings a
failure a green run cannot show, so the contracts need both what a label says
and how it was written; this module supplies both.

See Also
--------
tests.workflow_contracts.test_runner_placement : The rules built on these.
"""

from __future__ import annotations

import dataclasses as dc
import re
import typing as typ

from .runner_matrix import (
    collapse_label_whitespace,
    matrix_keys,
    matrix_values,
    sole_matrix_key,
)
from .runs_on_forms import declared_labels_in
from .workflow_support import (
    GITHUB_HOSTED_LABELS,
    WorkflowShapeError,
    WorkflowSource,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc

_RUNNER_EXPRESSION = re.compile(
    r"\$\{\{ *(?P<guard>.+?) *&& *'(?P<when_true>[^']*)'"
    r" *\|\| *'(?P<when_false>[^']*)' *\}\}"
)


class NotARunnerExpressionError(WorkflowShapeError):
    """A runner label was not a two-armed conditional expression.

    Parameters
    ----------
    raw : str
        The label as declared, quoted so a reader sees its whitespace.
    """

    def __init__(self, raw: str) -> None:
        super().__init__(f"{raw!r} is not a conditional runner label")


@dc.dataclass(frozen=True, slots=True)
class RunnerLabel:
    """One declared runner label, raw and parsed.

    The raw declaration is kept beside the parsed arms because the two answer
    different questions. The arms say which runner an event reaches; the raw
    text says whether the declaration survived YAML folding.

    Attributes
    ----------
    raw : str
        The label exactly as declared.
    guard : str
        The expression's condition, with folding whitespace collapsed.
    when_true : str
        The label used when the guard holds.
    when_false : str
        The label used otherwise.
    """

    raw: str
    guard: str
    when_true: str
    when_false: str


@dc.dataclass(frozen=True, slots=True)
class LabelRef:
    """One declared label, with enough context to name it in a failure.

    Attributes
    ----------
    workflow : str
        File name of the declaring workflow.
    job : str
        Key of the declaring job.
    source : str
        Where in the job it was declared, such as ``runs-on``.
    raw : str
        The label exactly as declared.
    """

    workflow: str
    job: str
    source: str
    raw: str

    @property
    def where(self) -> str:
        """A human-readable location for a failure message.

        Returns
        -------
        str
            The workflow, job and declaration site.
        """
        return f"{self.workflow}:{self.job}:{self.source}"


def runner_expression(raw: str) -> RunnerLabel:
    """Parse a two-armed conditional runner label.

    Folding whitespace is collapsed first, so a correctly wrapped
    continuation and a single-line declaration parse identically. Detecting a
    misplaced continuation is the raw text's business, not this function's.

    Parameters
    ----------
    raw : str
        The label as declared.

    Returns
    -------
    RunnerLabel
        The raw declaration with its guard and both arms.

    Raises
    ------
    NotARunnerExpressionError
        If the label is not a two-armed conditional expression.

    Examples
    --------
    >>> label = runner_expression(
    ...     "${{ github.event.pull_request.head.repo.fork"
    ...     " && 'ubuntu-latest' || 'ubicloud-standard-2' }}"
    ... )
    >>> label.guard
    'github.event.pull_request.head.repo.fork'
    >>> label.when_true, label.when_false
    ('ubuntu-latest', 'ubicloud-standard-2')
    """
    collapsed = collapse_label_whitespace(raw)
    match = _RUNNER_EXPRESSION.fullmatch(collapsed)
    if match is None:
        raise NotARunnerExpressionError(raw)
    return RunnerLabel(
        raw=raw,
        guard=match["guard"],
        when_true=match["when_true"],
        when_false=match["when_false"],
    )


def _runs_on_refs(
    workflow_name: str, job_name: str, job_document: cabc.Mapping[str, object]
) -> cabc.Iterator[LabelRef]:
    """Yield a job's own ``runs-on`` labels.

    GitHub accepts a single label, a list of them, or a mapping naming a
    runner group and labels. A job that calls a reusable workflow declares
    none, because the called workflow places its own jobs.

    Yields
    ------
    LabelRef
        Each label the job declares under ``runs-on``.
    """
    if "runs-on" not in job_document:
        return
    for source, label in declared_labels_in("runs-on", job_document["runs-on"]):
        yield LabelRef(workflow_name, job_name, source, label)


def _matrix_refs(
    workflow_name: str,
    job_name: str,
    declaration: LabelRef,
    job_document: cabc.Mapping[str, object],
) -> cabc.Iterator[LabelRef]:
    """Yield the labels a job's matrix supplies to one declaration.

    Yields
    ------
    LabelRef
        Each label the named axis supplies. A declaration composing a label
        from the matrix and anything else fails with
        :class:`runner_matrix.CompositeMatrixDeclarationError`.
    """
    key = sole_matrix_key(
        f"{workflow_name}:{job_name}:{declaration.source}", declaration.raw
    )
    if key is None:
        return
    for source, label in matrix_values(job_document, key):
        yield LabelRef(workflow_name, job_name, source, label)


def job_label_refs(
    workflow_name: str, job_name: str, job_document: cabc.Mapping[str, object]
) -> list[LabelRef]:
    """Return every label declaration one job carries.

    Parameters
    ----------
    workflow_name : str
        File name of the declaring workflow.
    job_name : str
        The job key.
    job_document : cabc.Mapping[str, object]
        The job mapping.

    Returns
    -------
    list[LabelRef]
        The ``runs-on`` declaration and, when it reads the matrix, the values
        the matrix supplies to it.
    """
    declared = list(_runs_on_refs(workflow_name, job_name, job_document))
    return [
        *declared,
        *(
            reference
            for declaration in declared
            for reference in _matrix_refs(
                workflow_name, job_name, declaration, job_document
            )
        ),
    ]


def declared_labels(source: WorkflowSource) -> list[LabelRef]:
    """Return every runner label the workflow estate declares.

    Covers the direct ``runs-on`` declaration and the matrix values a
    ``runs-on: ${{ matrix.os }}`` resolves from, because a label is in use
    whichever key it is written under.

    Parameters
    ----------
    source : WorkflowSource
        Where to read. Required: only the tests compose this repository's
        source, so no reader can fall back to it unnoticed.

    Returns
    -------
    list[LabelRef]
        Each declared label, with its workflow, job and declaration site.
    """
    found: list[LabelRef] = []
    for name in source.names():
        for job_name, job_document in source.jobs(name).items():
            found.extend(job_label_refs(name, job_name, job_document))
    return found


def resolve(reference: LabelRef) -> frozenset[str]:
    """Return the labels one declaration can select.

    A declaration that reads the matrix selects nothing by itself: its labels
    are declared in the matrix and reported as references of their own, so
    counting it here would add the literal text ``${{ matrix.os }}`` to the
    set of labels in use.

    An expression that is neither a matrix reference nor a two-armed
    conditional fails through the :class:`NotARunnerExpressionError` that
    `runner_expression` raises. It is raised rather than skipped: a skipped
    declaration leaves the registry equality holding over a smaller set than
    the estate actually uses, which is the one thing the equality exists to
    refuse.

    Parameters
    ----------
    reference : LabelRef
        One declaration.

    Returns
    -------
    frozenset[str]
        Every label the declaration can select.
    """
    raw = collapse_label_whitespace(reference.raw)
    if matrix_keys(raw):
        return frozenset()
    if not raw.startswith("${{"):
        return frozenset({raw})
    label = runner_expression(raw)
    return frozenset({label.when_true, label.when_false})


def job_labels(
    workflow_name: str, job_name: str, job_document: cabc.Mapping[str, object]
) -> frozenset[str]:
    """Return every label one job can run on.

    Parameters
    ----------
    workflow_name : str
        File name of the declaring workflow.
    job_name : str
        The job key.
    job_document : cabc.Mapping[str, object]
        The job mapping.

    Returns
    -------
    frozenset[str]
        The resolved labels, with both arms of a conditional counted because
        either can be selected.
    """
    resolved: set[str] = set()
    for reference in job_label_refs(workflow_name, job_name, job_document):
        resolved.update(resolve(reference))
    return frozenset(resolved)


def labels_in_use(source: WorkflowSource) -> set[str]:
    """Return every non-GitHub-hosted label the workflows resolve to.

    Parameters
    ----------
    source : WorkflowSource
        Where to read. Required: only the tests compose this repository's
        source, so no reader can fall back to it unnoticed.

    Returns
    -------
    set[str]
        The labels that need a registry entry.
    """
    resolved: set[str] = set()
    for reference in declared_labels(source):
        resolved.update(resolve(reference))
    return {label for label in resolved if label not in GITHUB_HOSTED_LABELS}
