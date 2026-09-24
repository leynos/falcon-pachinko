"""Reading a workflow's CodeScene interactions, and the lanes CV-005 names.

Split from the contracts so the scan can be driven over documents written for
one interaction each, and so the contracts and the placement readers share one
workflow source, one loader and one error hierarchy. Two hierarchies of the
same names in one package defeat the base class: ``except UnreadableWorkflowError``
catches whichever one the caller imported and misses the other.

Every query that reads the filesystem takes its source as a parameter; the
contracts compose this repository's.

See Also
--------
test_codescene_boundary : What a pull request may reach.
test_codescene_publisher : What the one publishing workflow must look like.
"""

from __future__ import annotations

import re
import typing as typ

from .pull_request_reach import pull_request_closure
from .workflow_support import NotAMappingError, WorkflowShapeError

if typ.TYPE_CHECKING:
    from .workflow_support import WorkflowSource

#: A full lowercase commit SHA, which is the only reference an action pin may
#: carry: a tag or a branch can be moved under the repository's feet.
FULL_SHA = re.compile(r"[0-9a-f]{40}")

#: The two shared actions the lanes call, named by their whole path. A step is
#: recognized only when the path before `@` is exactly one of these: a
#: substring match accepts `someone-else/upload-codescene-coverage` as the real
#: upload, so a repointed step would satisfy every rule written about it.
COVERAGE_ACTION: typ.Final[str] = (
    "leynos/shared-actions/.github/actions/generate-coverage"
)
UPLOAD_ACTION: typ.Final[str] = (
    "leynos/shared-actions/.github/actions/upload-codescene-coverage"
)

#: The workflow that owns the trunk generation and the upload.
PUBLISHER = "coverage-main.yml"
PUBLISHER_JOB = "coverage-upload"
#: The lane that serves pull requests.
PR_WORKFLOW = "ci.yml"
PR_JOB = "lint-test"

#: What a CodeScene interaction looks like, wherever it is written. Matched
#: against any scalar in the document: a credential can be written anywhere, a
#: `cs-coverage` invocation can be buried in a shell script, and the service's
#: host can be curled from one. The credential is named separately from the
#: action because a lane can be given it without calling the action, and a
#: credential a pull request's head can reach is what CV-005 is really about.
MARKERS: typ.Final[dict[str, str]] = {
    "a cs-coverage command": "cs-coverage",
    "the CodeScene credential": "CS_ACCESS_TOKEN",
    "the CodeScene service host": "codescene.io",
}
#: The action marker is scoped to `uses` values, and matches any CodeScene
#: action rather than the one this repository happens to call today. Applied
#: to every scalar it would report a step named "check CodeScene coverage" as
#: an invocation, and scoped to one action reference it would miss a second
#: CodeScene action entirely; both readings are wrong in the direction that
#: matters.
ACTION_MARKER: typ.Final[str] = "codescene"
ACTION_DESCRIPTION: typ.Final[str] = "a CodeScene action"
#: A job forwarding every secret its caller holds. It names no credential, so
#: no text marker finds it, and it is how the token reaches a reusable
#: workflow, remote or local, whose own text the caller never shows.
INHERITED_SECRETS: typ.Final[str] = "every secret forwarded by secrets: inherit"

#: Inputs that select what is measured. Publication differs between the two
#: lanes by design and is asserted separately.
SELECTION_INPUTS = ("output-path", "format", "pytest-workers", "with-ratchet")

#: Workflows allowed to trip a marker outside the publisher, each with the
#: reason and the single marker it may trip. Empty, and that is the point: the
#: publisher is now the only workflow in this repository that names CodeScene
#: at all. The table stays because the alternative to a per-marker exemption
#: is a per-file one, and a file-wide exemption would let an exempt workflow
#: acquire the credential, which is the thing the scan exists to find.
SCAN_EXEMPT: typ.Final[dict[str, tuple[str, str]]] = {}

#: The repository variable that held the CodeScene installer script's digest.
#: `installer-checksum` was its only consumer, and the shared action rejects
#: that input from f68e8e2e onwards, so a workflow reading this variable feeds
#: a rejected input and one refreshing it maintains a value nothing reads.
DEPRECATED_DIGEST_VARIABLE: typ.Final[str] = "CODESCENE_CLI_SHA256"


class MissingStepError(WorkflowShapeError):
    """A job does not declare a step or a steps list a contract names.

    Parameters
    ----------
    workflow : str
        The workflow searched.
    job : str
        The job searched.
    wanted : str
        What the contract needed from it.
    """

    def __init__(self, workflow: str, job: str, wanted: str) -> None:
        super().__init__(f"{workflow}:{job} must declare {wanted}")


def _walk(value: object, path: str) -> typ.Iterator[tuple[str, str]]:
    """Yield every scalar in a parsed document with the path that reached it.

    Parameters
    ----------
    value : object
        A parsed YAML fragment.
    path : str
        The path taken to reach it.

    Yields
    ------
    tuple[str, str]
        The path and the scalar rendered as text.
    """
    match value:
        case dict():
            for key, entry in value.items():
                yield f"{path}.{key}", str(key)
                yield from _walk(entry, f"{path}.{key}")
        case list():
            for position, entry in enumerate(value):
                yield from _walk(entry, f"{path}[{position}]")
        case _:
            yield path, str(value)


def _interactions_at(path: str, text: str) -> typ.Iterator[str]:
    """Yield each interaction one scalar carries.

    Parameters
    ----------
    path : str
        Where the scalar was found.
    text : str
        The scalar, rendered as text.

    Yields
    ------
    str
        One description per marker the scalar matches.
    """
    lowered = text.lower()
    for description, marker in MARKERS.items():
        if marker.lower() in lowered:
            yield f"{description} at {path}"
    if path.endswith(".uses") and ACTION_MARKER in lowered:
        yield f"{ACTION_DESCRIPTION} at {path}"
    if path.endswith(".secrets") and lowered.strip() == "inherit":
        yield f"{INHERITED_SECRETS} at {path}"


def references_in(document: object, subject: str) -> list[str]:
    """Return every place a parsed document interacts with CodeScene.

    The whole document is walked, not a list of step keys. A credential can be
    declared at workflow scope, at job scope, on a step, passed as an action
    input, interpolated into a ``run`` body, or forwarded by name, and a scan
    reading only ``uses`` and ``run`` would miss most of those. Forwarding by
    ``secrets: inherit`` names nothing, so it is recognized by its position.

    Pure, so the markers can be driven over documents written to carry one
    interaction each. Driven over this repository's workflows alone, a marker
    that had stopped matching would report a clean estate.

    Parameters
    ----------
    document : object
        A parsed workflow.
    subject : str
        What to name in the reported location.

    Returns
    -------
    list[str]
        One entry per interaction, naming what was found and where.
    """
    return sorted(
        {
            found
            for path, text in _walk(document, subject)
            for found in _interactions_at(path, text)
        }
    )


def triggers(source: WorkflowSource, name: str) -> dict[str, object]:
    """Return a workflow's trigger mapping, keyed as strings.

    The publisher's rules read filters inside the block, such as the push
    trigger's branches, so the scalar and list forms, which carry none, are
    refused here. :func:`pull_request_reach.trigger_names` reads all three
    forms where only the event names matter.

    Parameters
    ----------
    source : WorkflowSource
        Where to read.
    name : str
        The workflow file name.

    Returns
    -------
    dict[str, object]
        The declared events.

    Raises
    ------
    NotAMappingError
        If the trigger block is not a mapping.
    """
    document = source.document(name)
    declared = document.get("on", document.get(True))
    if not isinstance(declared, dict):
        raise NotAMappingError(f"{name}:on")
    return {str(key): value for key, value in declared.items()}


def steps(source: WorkflowSource, name: str, job_name: str) -> list[dict[str, object]]:
    """Return one job's steps.

    Parameters
    ----------
    source : WorkflowSource
        Where to read.
    name : str
        The workflow file name.
    job_name : str
        The job key.

    Returns
    -------
    list[dict[str, object]]
        Each step mapping, in declaration order.

    Raises
    ------
    MissingStepError
        If the job, or its steps list, is not declared.
    """
    job = source.jobs(name).get(job_name)
    declared = job.get("steps") if job is not None else None
    if not isinstance(declared, list):
        raise MissingStepError(name, job_name, "a steps list")
    return [step for step in declared if isinstance(step, dict)]


def declared_steps(
    source: WorkflowSource, name: str
) -> typ.Iterator[dict[str, object]]:
    """Yield every step of every job in one workflow.

    A job that calls a reusable workflow declares no steps at all, so a walk
    that demanded a steps list would fail on `dependabot-automerge.yml` rather
    than on anything these contracts are about.

    Parameters
    ----------
    source : WorkflowSource
        Where to read.
    name : str
        The workflow file name.

    Yields
    ------
    dict[str, object]
        Each declared step.
    """
    for job in source.jobs(name).values():
        declared = job.get("steps")
        if isinstance(declared, list):
            yield from (step for step in declared if isinstance(step, dict))


def invokes(step: dict[str, object], action: str) -> bool:
    """Return whether a step calls exactly ``action``, at whatever ref.

    Parameters
    ----------
    step : dict[str, object]
        One step mapping.
    action : str
        The action's full path, without a ref.

    Returns
    -------
    bool
        True when the step's ``uses`` names that path and nothing longer.
    """
    return str(step.get("uses", "")).partition("@")[0] == action


def coverage_inputs(
    source: WorkflowSource, name: str, job_name: str
) -> dict[str, object]:
    """Return the inputs of a job's coverage step.

    Parameters
    ----------
    source : WorkflowSource
        Where to read.
    name : str
        The workflow file name.
    job_name : str
        The job key.

    Returns
    -------
    dict[str, object]
        The ``with`` mapping of the step invoking ``generate-coverage``.

    Raises
    ------
    MissingStepError
        If the job has no coverage step, or the step declares no inputs.
    """
    for step in steps(source, name, job_name):
        if invokes(step, COVERAGE_ACTION):
            inputs = step.get("with")
            if not isinstance(inputs, dict):
                raise MissingStepError(name, job_name, "coverage inputs")
            return inputs
    raise MissingStepError(name, job_name, "a generate-coverage step")


def pull_request_workflows(source: WorkflowSource) -> list[str]:
    """Return every workflow a pull request can run.

    The closure through same-repository calls, not the trigger list: a
    ``workflow_call``-only workflow a pull-request job calls runs on that
    pull request and, under ``secrets: inherit``, holds every secret its
    caller does.

    Parameters
    ----------
    source : WorkflowSource
        Where to read.

    Returns
    -------
    list[str]
        Sorted workflow file names.
    """
    documents = {name: source.document(name) for name in source.names()}
    return sorted(pull_request_closure(documents))
