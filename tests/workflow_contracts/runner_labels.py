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

from .workflow_support import (
    GITHUB_HOSTED_LABELS,
    WorkflowShapeError,
    as_mapping,
    jobs,
    workflow_names,
)

# Collapses the folding whitespace a block scalar leaves in a label. Spelled
# out rather than written `\s`, which in Python also matches U+001C to
# U+001F; those are not whitespace to a runner label and must not be absorbed
# here.
_LABEL_WHITESPACE = re.compile(r"[ \t\n\r]+")
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


def collapse_label_whitespace(raw: str) -> str:
    r"""Collapse a label's folding whitespace to single spaces.

    A folded scalar leaves a space where it joined two lines. Contracts that
    assert what a label *says* read it through here, so only the contract
    asserting how it was *written* can fail on the difference.

    Parameters
    ----------
    raw : str
        The label as declared.

    Returns
    -------
    str
        The label with runs of spaces, tabs and line breaks collapsed.

    Examples
    --------
    >>> collapse_label_whitespace("${{ a\n      && 'x' }}")
    "${{ a && 'x' }}"
    """
    return _LABEL_WHITESPACE.sub(" ", raw).strip()


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
    workflow_name: str, job_name: str, job_document: dict[str, object]
) -> typ.Iterator[LabelRef]:
    """Yield a job's own ``runs-on`` labels.

    GitHub accepts a single label or a list of them, and a job that calls a
    reusable workflow declares neither.

    Yields
    ------
    LabelRef
        Each label the job declares under ``runs-on``.
    """
    match job_document.get("runs-on"):
        case str() as declared:
            yield LabelRef(workflow_name, job_name, "runs-on", declared)
        case list() as declared:
            for position, entry in enumerate(declared):
                if isinstance(entry, str):
                    yield LabelRef(
                        workflow_name, job_name, f"runs-on[{position}]", entry
                    )
        case _:
            return


def _matrix_include(job_document: dict[str, object]) -> list[object]:
    """Return a job's matrix include rows, or none when it declares no matrix.

    Parameters
    ----------
    job_document : dict[str, object]
        A job mapping.

    Returns
    -------
    list[object]
        The include rows, empty when the job declares no matrix or the matrix
        declares no include list.
    """
    strategy = as_mapping(job_document.get("strategy"))
    matrix = as_mapping(strategy.get("matrix")) if strategy else None
    include = matrix.get("include") if matrix else None
    if not isinstance(include, list):
        return []
    return list(include)


def _declared_os(row: object) -> str | None:
    """Return an include row's ``os`` label when it declares one.

    Parameters
    ----------
    row : object
        One matrix include row, which YAML permits to be anything.

    Returns
    -------
    str | None
        The declared label, or None when the row declares no string ``os``.
    """
    entry = as_mapping(row)
    label = entry.get("os") if entry else None
    return label if isinstance(label, str) else None


def _matrix_os_refs(
    workflow_name: str, job_name: str, job_document: dict[str, object]
) -> typ.Iterator[LabelRef]:
    """Return the ``os`` labels a job's matrix include rows declare.

    A ``runs-on: ${{ matrix.os }}`` resolves from these, so the labels a
    workflow really uses are not all written under ``runs-on``.

    Returns
    -------
    typ.Iterator[LabelRef]
        Each label an include row declares, in declaration order.
    """
    labels = (
        (position, _declared_os(row))
        for position, row in enumerate(_matrix_include(job_document))
    )
    return (
        LabelRef(
            workflow_name,
            job_name,
            f"strategy.matrix.include[{position}].os",
            label,
        )
        for position, label in labels
        if label is not None
    )


def declared_labels() -> list[LabelRef]:
    """Return every runner label the workflow estate declares.

    Covers the direct ``runs-on`` declaration and the matrix ``os`` values a
    ``runs-on: ${{ matrix.os }}`` resolves from, because a label is in use
    whichever key it is written under.

    Returns
    -------
    list[LabelRef]
        Each declared label, with its workflow, job and declaration site.
    """
    found: list[LabelRef] = []
    for name in workflow_names():
        for job_name, job_document in jobs(name).items():
            found.extend(_runs_on_refs(name, job_name, job_document))
            found.extend(_matrix_os_refs(name, job_name, job_document))
    return found


def labels_in_use() -> set[str]:
    """Return every non-GitHub-hosted label the workflows resolve to.

    Both arms of a conditional label count, because either can be selected.
    A label that is not a literal and not a two-armed conditional is left
    alone: the shape contract refuses those, and inventing a reading here
    would hide it.

    Returns
    -------
    set[str]
        The labels that need a registry entry.
    """
    resolved: set[str] = set()
    for reference in declared_labels():
        raw = collapse_label_whitespace(reference.raw)
        if raw.startswith("${{"):
            try:
                label = runner_expression(raw)
            except NotARunnerExpressionError:
                continue
            resolved.update({label.when_true, label.when_false})
        else:
            resolved.add(raw)
    return {label for label in resolved if label not in GITHUB_HOSTED_LABELS}
