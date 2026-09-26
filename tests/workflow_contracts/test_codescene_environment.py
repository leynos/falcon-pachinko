"""CV-005: the ``codescene`` environment sits on the uploading job alone.

The repository's own workflows are checked first. Each other test writes a
mutated copy of them into a temporary directory, the way a later edit could
change them, and asserts that the clause meant to catch it does. The check
step, the ref guard and ``access-token:`` stay held by the publisher and token
contracts.

Run with:

    uv run pytest tests/workflow_contracts
"""

from __future__ import annotations

import typing as typ

import yaml

from .codescene_environment import (
    MISSING,
    NO_UPLOADER,
    REACHABLE,
    STRAY,
    environment_violations,
)
from .codescene_scan import PR_JOB, PR_WORKFLOW, PUBLISHER, PUBLISHER_JOB
from .workflow_support import REPOSITORY, WorkflowSource

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    import pathlib

Documents = dict[str, dict[str, typ.Any]]


def _mutated(
    directory: pathlib.Path, mutate: cabc.Callable[[Documents], None]
) -> WorkflowSource:
    """Write this repository's workflows, changed by *mutate*, into *directory*.

    Parameters
    ----------
    directory : pathlib.Path
        A directory to write into, usually pytest's ``tmp_path``.
    mutate : Callable[[Documents], None]
        Edits the parsed documents in place.

    Returns
    -------
    WorkflowSource
        A source over the written copies.
    """
    documents = {
        name: yaml.safe_load(REPOSITORY.text(REPOSITORY.workflow_dir / name))
        for name in REPOSITORY.names()
    }
    mutate(documents)
    for name, document in documents.items():
        (directory / name).write_text(yaml.safe_dump(document), encoding="utf-8")
    return WorkflowSource(directory, REPOSITORY.actionlint_config)


def _job(documents: Documents, name: str, job: str) -> dict[str, typ.Any]:
    """Return one job mapping from the parsed documents."""
    return documents[name]["jobs"][job]


def _reports(source: WorkflowSource, message: str) -> bool:
    """Return whether the rule reports *message* over *source*."""
    return any(message in problem for problem in environment_violations(source))


def test_the_repository_places_the_environment() -> None:
    """The publisher declares the environment, and nothing else does."""
    assert environment_violations(REPOSITORY) == []


def test_the_publisher_cannot_drop_the_environment(tmp_path: pathlib.Path) -> None:
    """Without it the token never reaches the upload, which then skips."""

    def drop(documents: Documents) -> None:
        del _job(documents, PUBLISHER, PUBLISHER_JOB)["environment"]

    assert _reports(_mutated(tmp_path, drop), MISSING)


def test_the_publisher_cannot_name_another_environment(
    tmp_path: pathlib.Path,
) -> None:
    """Another environment holds no CodeScene token."""

    def rename(documents: Documents) -> None:
        _job(documents, PUBLISHER, PUBLISHER_JOB)["environment"] = "production"

    assert _reports(_mutated(tmp_path, rename), MISSING)


def test_the_mapping_form_is_accepted(tmp_path: pathlib.Path) -> None:
    """``{name: codescene}`` is the same declaration as the bare string."""

    def remap(documents: Documents) -> None:
        _job(documents, PUBLISHER, PUBLISHER_JOB)["environment"] = {"name": "codescene"}

    assert environment_violations(_mutated(tmp_path, remap)) == []


def test_no_other_job_may_declare_it(tmp_path: pathlib.Path) -> None:
    """A second holder of the token widens what can read it."""

    def add(documents: Documents) -> None:
        documents[PUBLISHER]["jobs"]["other"] = {
            "runs-on": "ubuntu-latest",
            "environment": "codescene",
            "steps": [{"run": "true"}],
        }

    assert _reports(_mutated(tmp_path, add), STRAY)


def test_no_pull_request_job_may_declare_it(tmp_path: pathlib.Path) -> None:
    """A pull request's own code must never be able to request the token."""

    def expose(documents: Documents) -> None:
        _job(documents, PR_WORKFLOW, PR_JOB)["environment"] = {"name": "codescene"}

    assert _reports(_mutated(tmp_path, expose), REACHABLE)


def test_an_empty_reading_is_refused(tmp_path: pathlib.Path) -> None:
    """With no uploader left the rule says so rather than passing."""

    def strip(documents: Documents) -> None:
        job = _job(documents, PUBLISHER, PUBLISHER_JOB)
        job["steps"] = [
            step
            for step in job["steps"]
            if "upload-codescene-coverage" not in str(step.get("uses", ""))
        ]

    assert _reports(_mutated(tmp_path, strip), NO_UPLOADER)
