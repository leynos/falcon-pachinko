"""Reading the runner labels a job's matrix supplies to its ``runs-on``.

Split from :mod:`runner_labels`, which reads what a job declares directly,
because a matrix reference is a different question: which values the named
axis carries, in both of the shapes a matrix may declare them.

A declaration reads the matrix only as exactly ``${{ matrix.<key> }}``.
GitHub interpolates a declaration such as ``${{ matrix.os }}-${{ matrix.arch
}}`` into one label per combination, and yielding each axis's values
separately would demand registrations for component labels no job requests
while omitting the label runner selection actually uses. Rendering every
combination is not needed by any workflow here, so the composite form is
refused rather than approximated.
"""

from __future__ import annotations

import re
import typing as typ

from .workflow_support import WorkflowShapeError, as_mapping

if typ.TYPE_CHECKING:
    import collections.abc as cabc

# Collapses the folding whitespace a block scalar leaves in a label. Spelled
# out rather than written `\s`, which in Python also matches U+001C to
# U+001F; those are not whitespace to a runner label and must not be absorbed
# here.
_LABEL_WHITESPACE = re.compile(r"[ \t\n\r]+")
# A ``runs-on`` that names a matrix key resolves its label from the matrix
# rather than declaring one. GitHub's context names allow letters, digits,
# hyphens and underscores.
_MATRIX_REFERENCE = re.compile(r"\bmatrix\.([A-Za-z_][A-Za-z0-9_-]*)")
_SOLE_MATRIX_REFERENCE = re.compile(r"\$\{\{ ?matrix\.([A-Za-z_][A-Za-z0-9_-]*) ?\}\}")


class CompositeMatrixDeclarationError(WorkflowShapeError):
    """A ``runs-on`` builds its label from the matrix and something else.

    Parameters
    ----------
    where : str
        The declaration site.
    raw : str
        The declaration as written.
    """

    def __init__(self, where: str, raw: str) -> None:
        super().__init__(
            f"{where}: {raw!r} composes a runner label from the matrix; only "
            "a declaration that is exactly one ${{ matrix.<key> }} is read, "
            "because the composed label is none of the axis values"
        )


class UnreadableMatrixValueError(WorkflowShapeError):
    """A matrix value a ``runs-on`` reads is not a label.

    Parameters
    ----------
    where : str
        The value's declaration site.
    value : object
        What was found.
    """

    def __init__(self, where: str, value: object) -> None:
        super().__init__(
            f"{where} is read as a runner label but declares {value!r}; "
            "refusing rather than dropping it from the labels in use"
        )


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


def sole_matrix_key(where: str, raw: str) -> str | None:
    """Return the one matrix key a declaration consists of, if it reads one.

    Parameters
    ----------
    where : str
        The declaration site, named in a refusal.
    raw : str
        The declaration as written.

    Returns
    -------
    str | None
        The key, or ``None`` when the declaration does not read the matrix.

    Raises
    ------
    CompositeMatrixDeclarationError
        If the declaration reads the matrix but is not exactly one reference.

    Examples
    --------
    >>> sole_matrix_key("runs-on", "${{ matrix.os }}")
    'os'
    """
    collapsed = collapse_label_whitespace(raw)
    if not matrix_keys(collapsed):
        return None
    sole = _SOLE_MATRIX_REFERENCE.fullmatch(collapsed)
    if sole is None:
        raise CompositeMatrixDeclarationError(where, raw)
    return sole[1]


def _matrix(job_document: cabc.Mapping[str, object]) -> cabc.Mapping[str, object]:
    """Return a job's matrix mapping, empty when it declares none.

    Parameters
    ----------
    job_document : cabc.Mapping[str, object]
        A job mapping.

    Returns
    -------
    cabc.Mapping[str, object]
        The matrix, or an empty mapping.
    """
    strategy = as_mapping(job_document.get("strategy"))
    matrix = as_mapping(strategy.get("matrix")) if strategy else None
    return matrix or {}


def _checked(site: str, value: object) -> tuple[str, str]:
    """Return a matrix value as a label, refusing any other kind.

    Parameters
    ----------
    site : str
        The value's declaration site.
    value : object
        The declared value.

    Returns
    -------
    tuple[str, str]
        The site and the label.

    Raises
    ------
    UnreadableMatrixValueError
        If the value is not a string.
    """
    if not isinstance(value, str):
        raise UnreadableMatrixValueError(site, value)
    return site, value


def _axis_sites(key: str, axis: object) -> list[tuple[str, str]]:
    """Return the labels a direct matrix axis declares, with their sites.

    Parameters
    ----------
    key : str
        The matrix key.
    axis : object
        The value declared under it, or ``None`` when there is none.

    Returns
    -------
    list[tuple[str, str]]
        The declaration site and the label for each entry.

    Raises
    ------
    UnreadableMatrixValueError
        If the axis is declared as something other than a list, such as an
        expression whose values the file does not state.
    """
    if axis is None:
        return []
    if not isinstance(axis, list):
        raise UnreadableMatrixValueError(f"strategy.matrix.{key}", axis)
    return [
        _checked(f"strategy.matrix.{key}[{position}]", entry)
        for position, entry in enumerate(axis)
    ]


def _include_sites(
    matrix: cabc.Mapping[str, object], key: str
) -> list[tuple[str, str]]:
    """Return the labels the ``include`` rows supply for one key.

    Parameters
    ----------
    matrix : cabc.Mapping[str, object]
        A job's matrix mapping.
    key : str
        The matrix key.

    Returns
    -------
    list[tuple[str, str]]
        The declaration site and the label for each row carrying the key.
    """
    include = matrix.get("include")
    rows = [as_mapping(row) for row in (include if isinstance(include, list) else ())]
    return [
        _checked(f"strategy.matrix.include[{position}].{key}", row[key])
        for position, row in enumerate(rows)
        if row is not None and key in row
    ]


def matrix_values(
    job_document: cabc.Mapping[str, object], key: str
) -> list[tuple[str, str]]:
    """Return every label one matrix key supplies, with its declaration site.

    Both shapes are read: the direct axis, which a matrix declares as a list
    under the key, and the ``include`` rows, which may add a value the axis
    does not carry. Reading only ``include`` would miss ``matrix: {os:
    [ubuntu-latest, windows-latest]}`` entirely, which is the commoner shape.

    Parameters
    ----------
    job_document : cabc.Mapping[str, object]
        A job mapping.
    key : str
        The matrix key the ``runs-on`` reads.

    Returns
    -------
    list[tuple[str, str]]
        The declaration site and the label, direct axis before include rows.

    Raises
    ------
    UnreadableMatrixValueError
        If nothing in the matrix supplies the key, because an empty answer
        would remove the job from every rule about labels. An axis that is
        not a list, and a value that is not a string, are refused by the
        helpers with the same error.
    """
    matrix = _matrix(job_document)
    found = [*_axis_sites(key, matrix.get(key)), *_include_sites(matrix, key)]
    if not found:
        raise UnreadableMatrixValueError(f"strategy.matrix.{key}", matrix.get(key))
    return found
