"""Loaders and constants shared by the workflow contracts.

The contracts read parsed documents and raw declarations from here rather
than each opening files of their own, so a change to the loading rules
reaches all of them.

The helpers raise :class:`WorkflowShapeError` subclasses instead of
asserting, so the module carries no blanket lint suppression and a malformed
workflow fails the same way whether or not assertions are enabled.
"""

from __future__ import annotations

import pathlib
import typing as typ

import yaml

#: Repository root, three levels above this file.
ROOT = pathlib.Path(__file__).resolve().parents[2]
WORKFLOW_DIR = ROOT / ".github" / "workflows"
ACTIONLINT_CONFIG = ROOT / ".github" / "actionlint.yaml"

#: The Ubicloud shape this repository deploys on.
UBICLOUD_LINUX_LABEL: typ.Final[str] = "ubicloud-standard-2"
#: GitHub-hosted labels. Frozen deliberately: the registry contract asks
#: which labels need registering, and the answer is "every label in use that
#: GitHub does not host", so this set is the exemption and is named, not
#: derived from a prefix. A prefix rule would silently exempt a second paid
#: provider's labels, which is the question the registry exists to ask.
GITHUB_HOSTED_LABELS: typ.Final[frozenset[str]] = frozenset(
    {"ubuntu-latest", "windows-latest", "macos-latest"}
)
GITHUB_HOSTED_LINUX: typ.Final[str] = "ubuntu-latest"
#: The field that says a pull request comes from a fork, and so cannot be
#: given an Ubicloud runner. Named separately from the whole expression
#: because the narrow mutation a contract must refuse swaps it for a sibling
#: field of the same object.
FORK_FIELD: typ.Final[str] = "github.event.pull_request.head.repo.fork"


class WorkflowShapeError(AssertionError):
    """A workflow is not shaped the way the contracts require.

    Derives from :class:`AssertionError` so a shape violation reads as a
    failed expectation rather than an unexpected crash.
    """


class UnparsableWorkflowError(WorkflowShapeError):
    """A workflow document is not parsable YAML.

    Parameters
    ----------
    name : str
        The workflow file that failed to parse.
    """

    def __init__(self, name: str) -> None:
        super().__init__(f"{name} must parse as YAML")


class NotAMappingError(WorkflowShapeError):
    """A document or fragment that should have parsed to a mapping did not.

    Parameters
    ----------
    subject : str
        What was being parsed, named in the message.
    """

    def __init__(self, subject: str) -> None:
        super().__init__(f"{subject} must parse to a mapping")


def workflow_names() -> list[str]:
    """Return every workflow file name, in sorted order.

    Returns
    -------
    list[str]
        File names such as ``ci.yml``, sorted so a failure lists them in a
        stable order.
    """
    return sorted(
        path.name for path in WORKFLOW_DIR.iterdir() if path.suffix in {".yml", ".yaml"}
    )


def as_mapping(value: object) -> dict[str, object] | None:
    """Return *value* as a string-keyed mapping, or None if it is not one.

    YAML keys are arbitrary scalars, not strings: a workflow's trigger block
    is keyed under the boolean ``True``, because YAML 1.1 reads the bare word
    ``on`` that way. Re-keying here keeps that fact in one place instead of
    letting every caller pretend the document is `dict[str, object]`.

    Parameters
    ----------
    value : object
        A parsed YAML value.

    Returns
    -------
    dict[str, object] | None
        The mapping with its keys rendered as strings, or None.

    Examples
    --------
    >>> as_mapping({True: "x"})
    {'True': 'x'}
    >>> as_mapping("not a mapping") is None
    True
    """
    if not isinstance(value, dict):
        return None
    return {str(key): entry for key, entry in value.items()}


def workflow(name: str) -> dict[object, object]:
    """Parse one workflow document.

    Parameters
    ----------
    name : str
        The workflow file name.

    Returns
    -------
    dict[object, object]
        The parsed document. Keyed by object, not by string: YAML 1.1 reads
        the bare word ``on`` as the boolean ``True``, so a workflow's trigger
        block is not under a string key at all.

    Raises
    ------
    UnparsableWorkflowError
        If the file is not parsable YAML.
    NotAMappingError
        If the document does not parse to a mapping.
    """
    text = (WORKFLOW_DIR / name).read_text(encoding="utf-8")
    try:
        document = yaml.safe_load(text)
    except yaml.YAMLError as error:
        raise UnparsableWorkflowError(name) from error
    if not isinstance(document, dict):
        raise NotAMappingError(name)
    return document


def jobs(name: str) -> dict[str, dict[str, object]]:
    """Return a workflow's jobs.

    Parameters
    ----------
    name : str
        The workflow file name.

    Returns
    -------
    dict[str, dict[str, object]]
        Each job key mapped to its document.

    Raises
    ------
    NotAMappingError
        If the workflow's ``jobs`` key is not a mapping.
    """
    declared = as_mapping(workflow(name).get("jobs"))
    if declared is None:
        raise NotAMappingError(f"{name}:jobs")
    found: dict[str, dict[str, object]] = {}
    for key, value in declared.items():
        document = as_mapping(value)
        if document is not None:
            found[key] = document
    return found
