"""Every shape a runner can be declared in is read, and every other refused.

A reader returning nothing for a shape it does not model removes that job from
the placement, ceiling and registry rules at once, and all three pass over it.
vk's reader did exactly that and hid a paid, unregistered sequence lane from
three contracts at once. So each form GitHub accepts is driven here over a job
written for it, and each form it does not accept, or that this reader will not
approximate, is shown to raise.

Run with:

    uv run pytest tests/workflow_contracts
"""

from __future__ import annotations

import typing as typ

import pytest

from .runner_labels import job_labels
from .runner_matrix import CompositeMatrixDeclarationError, UnreadableMatrixValueError
from .runs_on_forms import UnsupportedRunnerDeclarationError
from .test_label_reading import source_over
from .workflow_support import NotALabelError, NotAMappingError, UnparsableWorkflowError

if typ.TYPE_CHECKING:
    import pathlib


@pytest.mark.parametrize(
    ("declared", "expected"),
    [
        ("some-paid-runner", {"some-paid-runner"}),
        (["self-hosted", "some-paid-runner"], {"self-hosted", "some-paid-runner"}),
        ({"group": "paid-group"}, {"paid-group"}),
        ({"labels": "some-paid-runner"}, {"some-paid-runner"}),
        (
            {"group": "paid-group", "labels": ["linux", "x64"]},
            {"paid-group", "linux", "x64"},
        ),
    ],
    ids=["scalar", "sequence", "group", "labels scalar", "group and labels"],
)
def test_every_accepted_runs_on_form_is_read(
    declared: object, expected: set[str]
) -> None:
    """Read each form GitHub accepts, a runner group counting as a label.

    A group is as billable as a label and selects a runner the same way, so
    the registry and placement rules must see it.
    """
    labels = job_labels("ci.yml", "build", {"runs-on": declared})

    assert labels == expected, f"{declared!r} must read as {expected}; got {labels}"


def test_a_reusable_workflow_caller_declares_no_runner() -> None:
    """Read nothing from a job that calls a workflow, rather than refusing it."""
    job = {"uses": "./.github/workflows/build-wheels.yml"}

    assert job_labels("ci.yml", "call", job) == frozenset(), (
        "a caller places no job of its own"
    )


@pytest.mark.parametrize(
    "declared",
    [3, [], ["ubuntu-latest", 3], {}, {"os": "linux"}, {"labels": [["nested"]]}, None],
    ids=[
        "number",
        "empty list",
        "non-string entry",
        "empty mapping",
        "unknown key",
        "nested list",
        "null",
    ],
)
def test_an_unaccepted_runs_on_form_is_refused(declared: object) -> None:
    """Refuse rather than read an unmodelled shape as declaring no runner."""
    with pytest.raises(UnsupportedRunnerDeclarationError):
        job_labels("ci.yml", "build", {"runs-on": declared})


@pytest.mark.parametrize(
    "declared",
    [
        "${{ matrix.os }}-${{ matrix.arch }}",
        "${{ matrix.os }} ${{ matrix.arch }}",
        "x-${{ matrix.os }}",
    ],
    ids=["hyphenated", "spaced", "prefixed"],
)
def test_a_label_composed_from_the_matrix_is_refused(declared: str) -> None:
    """Refuse a declaration GitHub renders into a label no axis carries.

    Yielding each axis's values would demand registrations for components
    no job requests and omit the composed label runner selection uses.
    """
    job = {
        "runs-on": declared,
        "strategy": {"matrix": {"os": ["linux"], "arch": ["x64"]}},
    }

    with pytest.raises(CompositeMatrixDeclarationError):
        job_labels("ci.yml", "build", job)


def test_a_matrix_reference_inside_a_list_is_followed() -> None:
    """Resolve a matrix reference wherever a label may be written."""
    job = {
        "runs-on": ["self-hosted", "${{ matrix.os }}"],
        "strategy": {"matrix": {"os": ["some-paid-runner"]}},
    }

    labels = job_labels("ci.yml", "build", job)

    assert "some-paid-runner" in labels, (
        f"a matrix reference in a list must supply its axis; got {labels}"
    )


@pytest.mark.parametrize(
    "matrix",
    [
        {"os": "${{ fromJSON(inputs.runners) }}"},
        {"os": "${{ fromJSON(inputs.runners) }}", "include": [{"os": "extra"}]},
        {"python": ["3.13"]},
        {"os": [["self-hosted", "linux"]]},
        {"include": [{"os": 3}]},
    ],
    ids=[
        "expression axis",
        "expression axis beside an include row",
        "axis absent",
        "non-string value",
        "non-string include",
    ],
)
def test_an_unreadable_matrix_axis_is_refused(matrix: dict[str, object]) -> None:
    """Refuse an axis whose labels cannot be known from the file."""
    job = {"runs-on": "${{ matrix.os }}", "strategy": {"matrix": matrix}}

    with pytest.raises(UnreadableMatrixValueError):
        job_labels("ci.yml", "build", job)


def test_a_malformed_job_is_refused_not_omitted(tmp_path: pathlib.Path) -> None:
    """Refuse a job that is not a mapping instead of dropping it.

    Every traversal is built on the jobs this returns, so an omitted job is
    never checked by any of them.
    """
    source = source_over(tmp_path, {"ci.yml": {"jobs": {"good": {}, "bad": "x"}}})

    with pytest.raises(NotAMappingError, match=r"jobs\.bad"):
        source.jobs("ci.yml")


def test_a_non_string_registry_label_is_refused(tmp_path: pathlib.Path) -> None:
    """Refuse an entry the registry equality would otherwise never see."""
    registry = {"self-hosted-runner": {"labels": ["some-paid-runner", 3]}}
    source = source_over(tmp_path, {}, registry=registry)

    with pytest.raises(NotALabelError, match=r"labels\[1\]"):
        source.registered_labels()


@pytest.mark.parametrize(
    "text",
    [
        "jobs:\n  build:\n    runs-on: some-paid-runner\n    runs-on: ubuntu-latest\n",
        "? [a, b]\n: value\n",
    ],
    ids=["repeated key", "list as key"],
)
def test_a_document_pyyaml_would_misread_is_refused(
    tmp_path: pathlib.Path, text: str
) -> None:
    """Refuse a repeated key, which PyYAML resolves to the last in silence.

    With `runs-on` declared twice, the paid label in the discarded half would
    read as hosted and the placement rule would pass on a file that bills.
    """
    source = source_over(tmp_path, {})
    (source.workflow_dir / "ci.yml").write_text(text, encoding="utf-8")

    with pytest.raises(UnparsableWorkflowError):
        source.document("ci.yml")
