"""Reading the events a workflow declares, through an explicit source.

Split from the trigger contracts so the reader can be driven over a temporary
source as well as this repository's, and so the failures it propagates are
stated at one boundary rather than rediscovered by each caller.
"""

from __future__ import annotations

import typing as typ

from .workflow_support import NotAMappingError, as_mapping

if typ.TYPE_CHECKING:
    from .workflow_support import WorkflowSource

#: `on` is the YAML 1.1 boolean `True`, so a parsed workflow keys its trigger
#: block under the boolean and not the string. Reading `"on"` returns None
#: and every trigger contract then passes on an empty mapping.
ON_KEY = True


def triggers(source: WorkflowSource, workflow_name: str) -> dict[str, object]:
    """Return a workflow's trigger block.

    The placement contracts read filters inside the block, such as a push
    trigger's branches, so only the mapping form is accepted; a list or
    scalar trigger carries no filter to read and is refused.

    Parameters
    ----------
    source : WorkflowSource
        Where to read.
    workflow_name : str
        The workflow file name.

    Returns
    -------
    dict[str, object]
        The events the workflow declares, mapped to their filters.

    Raises
    ------
    NotAMappingError
        If the workflow declares no trigger mapping.

    Reading the document propagates the source's own refusals unchanged:
    :class:`~.workflow_support.UnreadableWorkflowError`,
    :class:`~.workflow_support.UndecodableWorkflowError`,
    :class:`~.workflow_support.UnparsableWorkflowError`, and
    :class:`~.workflow_support.NotAMappingError` for a document that is not a
    mapping.
    """
    declared = as_mapping(source.document(workflow_name).get(ON_KEY))
    if declared is None:
        raise NotAMappingError(f"{workflow_name}:on")
    return declared
