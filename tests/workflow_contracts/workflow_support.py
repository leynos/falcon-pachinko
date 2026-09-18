"""Loaders and constants shared by the workflow contracts.

The contracts read parsed documents and raw declarations from here rather
than each opening files of their own, so a change to the loading rules
reaches all of them.

Every read goes through a :class:`WorkflowSource`, which names the directory
it reads and the registry beside it rather than reaching for a module global.
The contracts use :data:`REPOSITORY`, this repository's own source; the tests
that prove the readers construct a source over a temporary directory, because
a rule parametrized over the very files it guards passes whether or not it
works.

Every failure the boundary can meet is translated into a documented exception
that derives from :class:`WorkflowShapeError`: a missing file, an unreadable
one, one that is not UTF-8, one that is not YAML, and one that is YAML but not
a mapping. A contract that meets any of them fails rather than raising a bare
``OSError`` from somewhere inside a comprehension.

The helpers raise :class:`WorkflowShapeError` subclasses instead of
asserting, so the module carries no blanket lint suppression and a malformed
workflow fails the same way whether or not assertions are enabled.
"""

from __future__ import annotations

import dataclasses as dc
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

#: Suffixes GitHub reads as workflow documents.
WORKFLOW_SUFFIXES: typ.Final[frozenset[str]] = frozenset({".yml", ".yaml"})


class WorkflowShapeError(AssertionError):
    """A workflow is not shaped the way the contracts require.

    Derives from :class:`AssertionError` so a shape violation reads as a
    failed expectation rather than an unexpected crash.
    """


class UnreadableWorkflowError(WorkflowShapeError):
    """A file the contracts must read could not be read.

    Covers a missing file, a directory that cannot be listed and a permission
    failure alike: to a contract they are the same event, an input it was
    promised and did not get.

    Parameters
    ----------
    path : pathlib.Path
        The path that could not be read.
    """

    def __init__(self, path: pathlib.Path) -> None:
        super().__init__(f"{path} must be readable")


class UndecodableWorkflowError(WorkflowShapeError):
    """A file the contracts must read was not valid UTF-8.

    Separate from :class:`UnreadableWorkflowError` because the remedy
    differs: the bytes arrived, and it is their encoding that is wrong.

    Parameters
    ----------
    path : pathlib.Path
        The path that could not be decoded.
    """

    def __init__(self, path: pathlib.Path) -> None:
        super().__init__(f"{path} must be UTF-8")


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


@dc.dataclass(frozen=True, slots=True)
class WorkflowSource:
    """A directory of workflow documents, with the registry that governs it.

    Named rather than assumed so the rules can be driven over a temporary
    directory holding a workflow written for one case. Driving them over
    ``.github/workflows`` alone would prove only that the current files pass,
    which they do whether or not the rule discriminates anything.

    Attributes
    ----------
    workflow_dir : pathlib.Path
        The directory holding the workflow documents.
    actionlint_config : pathlib.Path
        The `actionlint` configuration carrying the runner-label registry.
    """

    workflow_dir: pathlib.Path
    actionlint_config: pathlib.Path

    def names(self) -> list[str]:
        """Return every workflow file name, in sorted order.

        Returns
        -------
        list[str]
            File names such as ``ci.yml``, sorted so a failure lists them in
            a stable order.

        Raises
        ------
        UnreadableWorkflowError
            If the workflow directory cannot be listed.
        """
        try:
            entries = list(self.workflow_dir.iterdir())
        except OSError as error:
            raise UnreadableWorkflowError(self.workflow_dir) from error
        return sorted(path.name for path in entries if path.suffix in WORKFLOW_SUFFIXES)

    def text(self, path: pathlib.Path) -> str:
        """Read one file as UTF-8.

        Parameters
        ----------
        path : pathlib.Path
            The file to read.

        Returns
        -------
        str
            The file's decoded contents.

        Raises
        ------
        UnreadableWorkflowError
            If the file cannot be opened or read.
        UndecodableWorkflowError
            If the bytes are not valid UTF-8.
        """
        try:
            return path.read_text(encoding="utf-8")
        except UnicodeDecodeError as error:
            raise UndecodableWorkflowError(path) from error
        except OSError as error:
            raise UnreadableWorkflowError(path) from error

    def _load(self, path: pathlib.Path, subject: str) -> object:
        """Read and parse one YAML file.

        Parameters
        ----------
        path : pathlib.Path
            The file to read.
        subject : str
            What to name in a failure.

        Returns
        -------
        object
            Whatever the document parsed to.

        Raises
        ------
        UnparsableWorkflowError
            If the file is not parsable YAML.
        """
        text = self.text(path)
        try:
            return yaml.safe_load(text)
        except yaml.YAMLError as error:
            raise UnparsableWorkflowError(subject) from error

    def document(self, name: str) -> dict[object, object]:
        """Parse one workflow document.

        Parameters
        ----------
        name : str
            The workflow file name.

        Returns
        -------
        dict[object, object]
            The parsed document. Keyed by object, not by string: YAML 1.1
            reads the bare word ``on`` as the boolean ``True``, so a
            workflow's trigger block is not under a string key at all.

        Raises
        ------
        UnparsableWorkflowError
            If the file is not parsable YAML.
        NotAMappingError
            If the document does not parse to a mapping.
        """
        document = self._load(self.workflow_dir / name, name)
        if not isinstance(document, dict):
            raise NotAMappingError(name)
        # Copied rather than returned as parsed, so the declared type is true
        # rather than approximately true: the loader's result is untyped.
        return dict(document.items())

    def jobs(self, name: str) -> dict[str, dict[str, object]]:
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
        declared = as_mapping(self.document(name).get("jobs"))
        if declared is None:
            raise NotAMappingError(f"{name}:jobs")
        found: dict[str, dict[str, object]] = {}
        for key, value in declared.items():
            job_document = as_mapping(value)
            if job_document is not None:
                found[key] = job_document
        return found

    @staticmethod
    def _runner_section(parsed: object, subject: str) -> dict[str, object]:
        """Return the `actionlint` configuration's self-hosted-runner section.

        Parameters
        ----------
        parsed : object
            The parsed configuration.
        subject : str
            What to name in a failure.

        Returns
        -------
        dict[str, object]
            The section.

        Raises
        ------
        NotAMappingError
            If the configuration or the section is not a mapping.
        """
        top = as_mapping(parsed)
        if top is None:
            raise NotAMappingError(subject)
        section = as_mapping(top.get("self-hosted-runner"))
        if section is None:
            raise NotAMappingError(f"{subject}:self-hosted-runner")
        return section

    def registered_labels(self) -> list[str]:
        """Return the runner labels the `actionlint` registry declares.

        Routed through the source rather than read at its global path, so the
        registry contract can be driven over a temporary pair of directory
        and configuration.

        Returns
        -------
        list[str]
            The registered labels, in declaration order.

        Raises
        ------
        UnparsableWorkflowError
            If the configuration is not parsable YAML.
        NotAMappingError
            If the configuration, its ``self-hosted-runner`` section or its
            ``labels`` list is not shaped as `actionlint` requires.
        """
        subject = self.actionlint_config.name
        section = self._runner_section(
            self._load(self.actionlint_config, subject), subject
        )
        registered = section.get("labels")
        if not isinstance(registered, list):
            raise NotAMappingError(f"{subject}:self-hosted-runner.labels")
        return [entry for entry in registered if isinstance(entry, str)]


#: This repository's own workflows and registry. The contracts read through
#: this; the tests that prove the readers build their own.
REPOSITORY: typ.Final[WorkflowSource] = WorkflowSource(WORKFLOW_DIR, ACTIONLINT_CONFIG)
