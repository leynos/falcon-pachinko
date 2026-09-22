"""Drive the trigger reader over a temporary source.

The trigger contracts read this repository, which passes a broken reader as
readily as a working one. Each failure the reader can meet is provoked here
over a source written for it, so the reader's boundary is proved rather than
described.

Run with:

    uv run pytest tests/workflow_contracts
"""

from __future__ import annotations

import typing as typ

import pytest

from .lane_triggers import triggers
from .test_label_reading import source_over
from .workflow_support import (
    NotAMappingError,
    UndecodableWorkflowError,
    UnparsableWorkflowError,
    UnreadableWorkflowError,
)

if typ.TYPE_CHECKING:
    import pathlib


def test_the_trigger_block_is_read_from_the_source_given(
    tmp_path: pathlib.Path,
) -> None:
    """Read the source passed in, not this repository's."""
    source = source_over(tmp_path, {"lane.yml": {True: {"push": {"branches": ["x"]}}}})

    assert triggers(source, "lane.yml") == {"push": {"branches": ["x"]}}, (
        "the reader must return the temporary workflow's trigger block"
    )


@pytest.mark.parametrize(
    "document",
    [{"jobs": {}}, {True: ["push", "pull_request"]}, {True: "push"}],
    ids=["absent", "list form", "scalar form"],
)
def test_a_trigger_block_without_filters_is_refused(
    tmp_path: pathlib.Path, document: dict[object, object]
) -> None:
    """Refuse the forms that carry no filters where filters are read."""
    source = source_over(tmp_path, {"lane.yml": document})

    with pytest.raises(NotAMappingError, match=r"lane\.yml:on"):
        triggers(source, "lane.yml")


@pytest.mark.parametrize(
    ("contents", "error"),
    [
        (b"\xff\xfe not utf-8", UndecodableWorkflowError),
        (b"on: [unclosed\n", UnparsableWorkflowError),
    ],
    ids=["undecodable", "unparsable"],
)
def test_a_workflow_the_source_cannot_read_propagates_its_error(
    tmp_path: pathlib.Path, contents: bytes, error: type[Exception]
) -> None:
    """Propagate the source's own refusal, not a raw decoding or parse error."""
    source = source_over(tmp_path, {})
    (source.workflow_dir / "lane.yml").write_bytes(contents)

    with pytest.raises(error):
        triggers(source, "lane.yml")


def test_an_absent_workflow_propagates_the_unreadable_error(
    tmp_path: pathlib.Path,
) -> None:
    """Refuse a name the source does not hold as unreadable, naming it."""
    source = source_over(tmp_path, {})

    with pytest.raises(UnreadableWorkflowError, match=r"lane\.yml"):
        triggers(source, "lane.yml")
