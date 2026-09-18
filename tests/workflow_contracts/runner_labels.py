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
    REPOSITORY,
    WorkflowShapeError,
    WorkflowSource,
    as_mapping,
)

# Collapses the folding whitespace a block scalar leaves in a label. Spelled
# out rather than written `\s`, which in Python also matches U+001C to
# U+001F; those are not whitespace to a runner label and must not be absorbed
# here.
_LABEL_WHITESPACE = re.compile(r"[ \t\n\r]+")
# A ``runs-on`` that names a matrix key resolves its label from the matrix
# rather than declaring one. GitHub's context names allow letters, digits,
# hyphens and underscores.
_MATRIX_REFERENCE = re.compile(r"\bmatrix\.([A-Za-z_][A-Za-z0-9_-]*)")
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


def matrix_keys(raw: str) -> frozenset[str]:
    """Return the matrix keys a ``runs-on`` declaration resolves from.

    A job whose ``runs-on`` never mentions the matrix resolves no label from
    it, however many axes the matrix declares. Reading every ``os`` in sight
    instead would let an unrelated test parameter named ``os`` register as a
    runner label, and the registry equality would then fail on a label no job
    can ever request.

    Parameters
    ----------
    raw : str
        The label as declared.

    Returns
    -------
    frozenset[str]
        Each matrix key the declaration reads.

    Examples
    --------
    >>> sorted(matrix_keys("${{ matrix.os }}"))
    ['os']
    >>> matrix_keys("ubuntu-latest")
    frozenset()
    """
    return frozenset(_MATRIX_REFERENCE.findall(raw))


def is_matrix_reference(raw: str) -> bool:
    """Say whether a declaration resolves its label from the matrix.

    Parameters
    ----------
    raw : str
        The label as declared.

    Returns
    -------
    bool
        True when the declaration reads at least one matrix key.

    Examples
    --------
    >>> is_matrix_reference("${{ matrix.os }}")
    True
    >>> is_matrix_reference("ubicloud-standard-2")
    False
    """
    return bool(matrix_keys(collapse_label_whitespace(raw)))


def _matrix(job_document: dict[str, object]) -> dict[str, object]:
    """Return a job's matrix mapping, empty when it declares none.

    Parameters
    ----------
    job_document : dict[str, object]
        A job mapping.

    Returns
    -------
    dict[str, object]
        The matrix, or an empty mapping.
    """
    strategy = as_mapping(job_document.get("strategy"))
    matrix = as_mapping(strategy.get("matrix")) if strategy else None
    return matrix or {}


def _axis_values(axis: object, key: str) -> typ.Iterator[tuple[str, str]]:
    """Yield a direct matrix axis's string values with their sites.

    Parameters
    ----------
    axis : object
        The value declared under the matrix key, which YAML permits to be
        anything.
    key : str
        The matrix key, named in the declaration site.

    Yields
    ------
    tuple[str, str]
        The declaration site and the label.
    """
    if not isinstance(axis, list):
        return
    for position, entry in enumerate(axis):
        if isinstance(entry, str):
            yield f"strategy.matrix.{key}[{position}]", entry


def _include_values(
    matrix: dict[str, object], key: str
) -> typ.Iterator[tuple[str, str]]:
    """Yield the include rows' values for one matrix key, with their sites.

    Parameters
    ----------
    matrix : dict[str, object]
        A job's matrix mapping.
    key : str
        The matrix key to read from each row.

    Yields
    ------
    tuple[str, str]
        The declaration site and the label.
    """
    include = matrix.get("include")
    if not isinstance(include, list):
        return
    for position, row in enumerate(include):
        entry = as_mapping(row)
        value = entry.get(key) if entry else None
        if isinstance(value, str):
            yield f"strategy.matrix.include[{position}].{key}", value


def _matrix_refs(
    workflow_name: str, job_name: str, job_document: dict[str, object]
) -> typ.Iterator[LabelRef]:
    """Yield the labels a job's matrix supplies to its ``runs-on``.

    Only the keys the ``runs-on`` declaration actually reads are followed,
    and for each of those both shapes are read: the direct axis, which a
    matrix declares as a list under the key, and the ``include`` rows, which
    may add a value the axis does not carry. Reading only ``include`` would
    miss ``matrix: {os: [ubuntu-latest, windows-latest]}`` entirely, which is
    the commoner of the two shapes.

    Yields
    ------
    LabelRef
        Each label the matrix supplies, direct axis before include rows.
    """
    declared = job_document.get("runs-on")
    if not isinstance(declared, str):
        return
    matrix = _matrix(job_document)
    for key in sorted(matrix_keys(collapse_label_whitespace(declared))):
        for source, label in _axis_values(matrix.get(key), key):
            yield LabelRef(workflow_name, job_name, source, label)
        for source, label in _include_values(matrix, key):
            yield LabelRef(workflow_name, job_name, source, label)


def job_label_refs(
    workflow_name: str, job_name: str, job_document: dict[str, object]
) -> list[LabelRef]:
    """Return every label declaration one job carries.

    Parameters
    ----------
    workflow_name : str
        File name of the declaring workflow.
    job_name : str
        The job key.
    job_document : dict[str, object]
        The job mapping.

    Returns
    -------
    list[LabelRef]
        The ``runs-on`` declaration and, when it reads the matrix, the values
        the matrix supplies to it.
    """
    return [
        *_runs_on_refs(workflow_name, job_name, job_document),
        *_matrix_refs(workflow_name, job_name, job_document),
    ]


def declared_labels(source: WorkflowSource = REPOSITORY) -> list[LabelRef]:
    """Return every runner label the workflow estate declares.

    Covers the direct ``runs-on`` declaration and the matrix values a
    ``runs-on: ${{ matrix.os }}`` resolves from, because a label is in use
    whichever key it is written under.

    Parameters
    ----------
    source : WorkflowSource
        Where to read. Defaults to this repository.

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

    Parameters
    ----------
    reference : LabelRef
        One declaration.

    Returns
    -------
    frozenset[str]
        Every label the declaration can select.

    Raises
    ------
    NotARunnerExpressionError
        If the declaration is an expression that is neither a matrix
        reference nor a two-armed conditional. Raised rather than skipped:
        a skipped declaration leaves the registry equality holding over a
        smaller set than the estate actually uses, which is the one thing
        the equality exists to refuse.
    """
    raw = collapse_label_whitespace(reference.raw)
    if matrix_keys(raw):
        return frozenset()
    if not raw.startswith("${{"):
        return frozenset({raw})
    label = runner_expression(raw)
    return frozenset({label.when_true, label.when_false})


def job_labels(
    workflow_name: str, job_name: str, job_document: dict[str, object]
) -> frozenset[str]:
    """Return every label one job can run on.

    Parameters
    ----------
    workflow_name : str
        File name of the declaring workflow.
    job_name : str
        The job key.
    job_document : dict[str, object]
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


def labels_in_use(source: WorkflowSource = REPOSITORY) -> set[str]:
    """Return every non-GitHub-hosted label the workflows resolve to.

    Parameters
    ----------
    source : WorkflowSource
        Where to read. Defaults to this repository.

    Returns
    -------
    set[str]
        The labels that need a registry entry.
    """
    resolved: set[str] = set()
    for reference in declared_labels(source):
        resolved.update(resolve(reference))
    return {label for label in resolved if label not in GITHUB_HOSTED_LABELS}
