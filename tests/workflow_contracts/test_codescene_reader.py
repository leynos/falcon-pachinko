"""Driving the workflow reader over directories written for the case.

The contracts beside this file read `.github/workflows`. That shows the estate
passes today; it cannot show that a reader discriminates anything, because the
estate passes a broken reader too. A reader that swallowed an I/O failure, or
returned an empty list for a missing directory, would leave every one of those
contracts green while checking nothing.

So the reader is driven here over temporary directories, and every failure its
boundary can meet is provoked rather than described.

Run with:

    uv run pytest tests/workflow_contracts
"""

from __future__ import annotations

import typing as typ

import pytest
import yaml

from .codescene_scan import pull_request_workflows
from .workflow_support import (
    REPOSITORY,
    NotAMappingError,
    UndecodableWorkflowError,
    UnparsableWorkflowError,
    UnreadableWorkflowError,
    WorkflowSource,
)

if typ.TYPE_CHECKING:
    import pathlib


def source_over(
    directory: pathlib.Path, documents: dict[str, object]
) -> WorkflowSource:
    """Write *documents* into *directory* and return a source reading them.

    Parameters
    ----------
    directory : pathlib.Path
        A directory to write into, usually pytest's ``tmp_path``.
    documents : dict[str, object]
        File name mapped to the document to dump under it.

    Returns
    -------
    WorkflowSource
        A source over the written directory.
    """
    for name, document in documents.items():
        (directory / name).write_text(yaml.safe_dump(document), encoding="utf-8")
    return WorkflowSource(directory, directory / "actionlint.yaml")


def test_a_missing_directory_fails_as_a_contract_failure(
    tmp_path: pathlib.Path,
) -> None:
    """Translate an absent directory rather than raising from a comprehension.

    Every contract walks the directory. If it is not there the suite should say
    so in those words, not surface an ``OSError`` from inside a generator, and
    above all not return an empty list, which would clear the estate by finding
    no workflows at all.
    """
    source = WorkflowSource(tmp_path / "absent", tmp_path / "actionlint.yaml")

    with pytest.raises(UnreadableWorkflowError):
        source.names()


def test_a_missing_file_fails_as_a_contract_failure(tmp_path: pathlib.Path) -> None:
    """Name the file that was promised and not delivered."""
    source = source_over(tmp_path, {})

    with pytest.raises(UnreadableWorkflowError):
        source.document("ci.yml")


def test_a_non_utf8_workflow_fails_as_a_contract_failure(
    tmp_path: pathlib.Path,
) -> None:
    """Distinguish bytes that did not arrive from bytes that will not decode."""
    source = source_over(tmp_path, {})
    (tmp_path / "ci.yml").write_bytes(b"name: \xff\xfe")

    with pytest.raises(UndecodableWorkflowError):
        source.document("ci.yml")


def test_an_unparsable_workflow_names_the_file(tmp_path: pathlib.Path) -> None:
    """Name the file that would not parse, since the traversal reads many."""
    source = source_over(tmp_path, {})
    (tmp_path / "ci.yml").write_text("jobs: [unclosed\n", encoding="utf-8")

    with pytest.raises(UnparsableWorkflowError, match=r"ci\.yml"):
        source.document("ci.yml")


def test_a_workflow_that_is_not_a_mapping_is_refused(tmp_path: pathlib.Path) -> None:
    """Refuse a document that parses but is not a workflow."""
    source = source_over(tmp_path, {})
    (tmp_path / "ci.yml").write_text("just a string\n", encoding="utf-8")

    with pytest.raises(NotAMappingError):
        source.document("ci.yml")


def test_the_reader_lists_only_workflow_documents(tmp_path: pathlib.Path) -> None:
    """Read both extensions GitHub accepts, and nothing else.

    A scan that read only ``.yml`` would clear a workflow written as ``.yaml``,
    which GitHub runs exactly the same way.
    """
    source = source_over(tmp_path, {"ci.yml": {"jobs": {}}, "other.yaml": {"jobs": {}}})
    (tmp_path / "notes.md").write_text("not a workflow\n", encoding="utf-8")

    assert source.names() == ["ci.yml", "other.yaml"]


def test_the_repository_source_is_this_repository() -> None:
    """Keep the composed source reading the estate the contracts are about.

    The rules above pass a source of their own. If the repository's source
    had drifted to one of those, every other contract in this directory would
    be asserting something about a temporary directory.
    """
    found = REPOSITORY.names()

    assert "ci.yml" in found, f"the source must read this repository: {found}"
    assert "coverage-main.yml" in found, f"the source must reach the publisher: {found}"


def test_the_boundary_scans_the_closure_of_a_source(tmp_path: pathlib.Path) -> None:
    """Drive the composition the boundary rule uses, not only its parts.

    The closure is proved on its own elsewhere; this proves the query the
    contracts call reaches a workflow only a call reaches. This repository
    calls no local reusable workflow, so read from its own files a query that
    had fallen back to the trigger list would pass.
    """
    source = source_over(
        tmp_path,
        {
            "gate.yml": {
                True: {"pull_request": None},
                "jobs": {"call": {"uses": "./.github/workflows/helper.yml"}},
            },
            "helper.yml": {True: {"workflow_call": None}, "jobs": {}},
        },
    )

    reached = pull_request_workflows(source)

    assert reached == ["gate.yml", "helper.yml"], (
        f"the boundary must scan the called workflow too; it scans {reached}"
    )
