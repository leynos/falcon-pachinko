"""The shapes GitHub accepts for a ``runs-on``, read or refused.

Split from :mod:`runner_labels`, which turns declarations into labels in use,
because this module answers only what one ``runs-on`` value declares. All
three forms GitHub accepts are read: a label, a list of labels, and a mapping
of ``group`` and ``labels``. Anything else is refused rather than read as
declaring no runner, because a job that declares none drops out of the
placement, ceiling and registry rules at once and all three then pass over
it; vk's reader hid a paid, unregistered sequence lane from three contracts
that way.
"""

from __future__ import annotations

import typing as typ

from .workflow_support import WorkflowShapeError

if typ.TYPE_CHECKING:
    import collections.abc as cabc

#: The keys GitHub reads from a mapping-form ``runs-on``. A runner group is
#: as billable as a label, so both are declarations.
_RUNS_ON_MAPPING_KEYS = ("group", "labels")


class UnsupportedRunnerDeclarationError(WorkflowShapeError):
    """A ``runs-on`` is in no shape GitHub accepts, or none this reads.

    Refused rather than read as declaring no runner: a job that declares none
    drops out of the placement, ceiling and registry rules at once, and all
    three then pass over it.

    Parameters
    ----------
    where : str
        The declaration site.
    value : object
        What was found.
    """

    def __init__(self, where: str, value: object) -> None:
        super().__init__(
            f"{where} declares {value!r}; runs-on must be a label, a list of "
            "labels, or a mapping of group and labels"
        )


def _label_list(where: str, declared: object) -> cabc.Iterator[tuple[str, str]]:
    """Yield a label, or each label of a non-empty list of them.

    Parameters
    ----------
    where : str
        The declaration site.
    declared : object
        A label or a list of labels.

    Yields
    ------
    tuple[str, str]
        Each declaration site and the label written there.

    Raises
    ------
    UnsupportedRunnerDeclarationError
        If the value, or any entry in it, is not a label.
    """
    match declared:
        case str():
            yield where, declared
        case list() if declared:
            for position, entry in enumerate(declared):
                if not isinstance(entry, str):
                    raise UnsupportedRunnerDeclarationError(
                        f"{where}[{position}]", entry
                    )
                yield f"{where}[{position}]", entry
        case _:
            raise UnsupportedRunnerDeclarationError(where, declared)


def _mapping_labels(
    where: str, declared: dict[object, object]
) -> cabc.Iterator[tuple[str, str]]:
    """Yield the labels a ``group``/``labels`` mapping declares.

    Parameters
    ----------
    where : str
        The declaration site.
    declared : dict[object, object]
        The mapping.

    Yields
    ------
    tuple[str, str]
        The group, as one label, then each entry of ``labels``.

    Raises
    ------
    UnsupportedRunnerDeclarationError
        If the mapping is empty, names another key, or its group is not one
        label.
    """
    if not declared or not set(declared) <= set(_RUNS_ON_MAPPING_KEYS):
        raise UnsupportedRunnerDeclarationError(where, declared)
    if "group" in declared:
        group = declared["group"]
        if not isinstance(group, str):
            raise UnsupportedRunnerDeclarationError(f"{where}.group", group)
        yield f"{where}.group", group
    if "labels" in declared:
        yield from _label_list(f"{where}.labels", declared["labels"])


def declared_labels_in(where: str, declared: object) -> cabc.Iterator[tuple[str, str]]:
    """Yield the labels one ``runs-on`` value declares, in any accepted form.

    A label, a non-empty list of labels, or a mapping naming a ``group`` and
    ``labels``; :func:`_label_list` and :func:`_mapping_labels` refuse
    anything else with :class:`UnsupportedRunnerDeclarationError`.

    Parameters
    ----------
    where : str
        The declaration site.
    declared : object
        The ``runs-on`` value.

    Yields
    ------
    tuple[str, str]
        Each declaration site and the label written there.
    """
    if isinstance(declared, dict):
        yield from _mapping_labels(where, declared)
    else:
        yield from _label_list(where, declared)
