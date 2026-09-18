"""Driving the label readers over workflows written for the case.

The contracts beside this file read `.github/workflows`. That proves the
estate passes today; it cannot prove a rule discriminates anything, because
the estate passes the broken rule too. A reader tied to the wrong matrix key,
or blind to a direct matrix axis, or silently skipping a label it cannot
parse, all leave those contracts green.

So the readers are driven here over documents built for one question each,
through a :class:`~tests.workflow_contracts.workflow_support.WorkflowSource`
over a temporary directory. Every failure mode the boundary translates is
provoked rather than described.

Run with:

    uv run pytest tests/workflow_contracts
"""

from __future__ import annotations

import typing as typ

import pytest
import yaml

from .runner_labels import (
    LabelRef,
    NotARunnerExpressionError,
    job_label_refs,
    job_labels,
    labels_in_use,
    matrix_keys,
    resolve,
)
from .workflow_support import (
    NotAMappingError,
    UndecodableWorkflowError,
    UnparsableWorkflowError,
    UnreadableWorkflowError,
    WorkflowSource,
)

if typ.TYPE_CHECKING:
    import pathlib

FORK_EXPRESSION = (
    "${{ github.event.pull_request.head.repo.fork"
    " && 'ubuntu-latest' || 'ubicloud-standard-2' }}"
)


def source_over(
    directory: pathlib.Path, workflows: dict[str, object], registry: object = None
) -> WorkflowSource:
    """Write *workflows* into *directory* and return a source reading them.

    Parameters
    ----------
    directory : pathlib.Path
        A directory to write into, usually pytest's ``tmp_path``.
    workflows : dict[str, object]
        File name mapped to the document to dump under it.
    registry : object
        The `actionlint` configuration to write beside them, or None to
        write no configuration at all.

    Returns
    -------
    WorkflowSource
        A source over the written directory.
    """
    workflow_dir = directory / "workflows"
    workflow_dir.mkdir(exist_ok=True)
    for name, document in workflows.items():
        (workflow_dir / name).write_text(yaml.safe_dump(document), encoding="utf-8")
    config = directory / "actionlint.yaml"
    if registry is not None:
        config.write_text(yaml.safe_dump(registry), encoding="utf-8")
    return WorkflowSource(workflow_dir, config)


def test_an_unrelated_matrix_parameter_is_not_a_runner_label() -> None:
    """Refuse a label read from a matrix the job's ``runs-on`` never consults.

    A job can name its runner outright and still carry an `include` row with
    an `os` key, as a test parameter naming a platform to exercise. Reading
    every `os` in sight would enter that value into the labels in use, and
    the registry equality would then demand a registration for a runner no
    job can request.
    """
    job = {
        "runs-on": "ubuntu-latest",
        "strategy": {"matrix": {"include": [{"os": "some-paid-runner", "py": "3.13"}]}},
    }

    assert job_labels("ci.yml", "lint-test", job) == frozenset({"ubuntu-latest"})


def test_a_direct_matrix_axis_supplies_labels() -> None:
    """Read the commoner of the two matrix shapes.

    ``matrix: {os: [...]}`` needs no `include` at all, and a reader that
    looked only at `include` rows would find no labels for such a job and
    leave every one of them unregistered.
    """
    job = {
        "runs-on": "${{ matrix.os }}",
        "strategy": {"matrix": {"os": ["ubuntu-latest", "some-paid-runner"]}},
    }

    assert job_labels("ci.yml", "build", job) == frozenset(
        {"ubuntu-latest", "some-paid-runner"}
    )


def test_a_matrix_axis_and_its_include_rows_are_both_read() -> None:
    """Read a label an `include` row adds to a declared axis.

    An `include` row may carry a value the axis does not, which is how a
    matrix grows a leg without restating the whole axis.
    """
    job = {
        "runs-on": "${{ matrix.os }}",
        "strategy": {
            "matrix": {
                "os": ["ubuntu-latest"],
                "include": [{"os": "macos-latest", "arch": "aarch64"}],
            }
        },
    }

    assert job_labels("wheels.yml", "build", job) == frozenset(
        {"ubuntu-latest", "macos-latest"}
    )


def test_the_matrix_key_read_is_the_one_the_declaration_names() -> None:
    """Follow the key named, not a key named ``os`` by convention.

    A matrix axis may be called anything. A reader hard-coded to ``os``
    reports nothing for a job keyed on ``runner`` and registers its labels
    nowhere.
    """
    job = {
        "runs-on": "${{ matrix.runner }}",
        "strategy": {"matrix": {"runner": ["some-paid-runner"], "os": ["ignored"]}},
    }

    assert job_labels("ci.yml", "build", job) == frozenset({"some-paid-runner"})


def test_a_matrix_reference_names_its_declaration_site() -> None:
    """Name where a matrix-supplied label was written.

    A failure that said only "some-paid-runner is unregistered" would leave
    the reader searching a matrix for it.
    """
    job = {
        "runs-on": "${{ matrix.os }}",
        "strategy": {"matrix": {"os": ["some-paid-runner"]}},
    }

    sites = [reference.where for reference in job_label_refs("ci.yml", "build", job)]

    assert sites == [
        "ci.yml:build:runs-on",
        "ci.yml:build:strategy.matrix.os[0]",
    ]


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("ubicloud-standard-2", frozenset({"ubicloud-standard-2"})),
        (FORK_EXPRESSION, frozenset({"ubuntu-latest", "ubicloud-standard-2"})),
        ("${{ matrix.os }}", frozenset()),
    ],
    ids=["literal", "both conditional arms", "matrix reference selects nothing"],
)
def test_resolution_covers_each_declaration_shape(
    raw: str, expected: frozenset[str]
) -> None:
    """Resolve each shape a ``runs-on`` can take.

    Both arms of a conditional count because either can be selected. A matrix
    reference resolves to nothing here because its labels are declared in the
    matrix and reported as references of their own; counting it would enter
    the literal text of the expression into the labels in use.
    """
    assert resolve(LabelRef("ci.yml", "lint-test", "runs-on", raw)) == expected


def test_an_unparsable_expression_is_raised_rather_than_skipped() -> None:
    """Refuse to let an unreadable label shrink the set the registry meets.

    Skipping it would leave the registry equality holding over a smaller set
    than the estate really uses, so an unregistered label would pass by being
    written in a shape the reader cannot follow. That is the one thing the
    equality exists to refuse.
    """
    reference = LabelRef("ci.yml", "lint-test", "runs-on", "${{ some.other.thing }}")

    with pytest.raises(NotARunnerExpressionError):
        resolve(reference)


def test_labels_in_use_exempts_only_the_named_hosted_labels(
    tmp_path: pathlib.Path,
) -> None:
    """Report every label GitHub does not host, from wherever it is written.

    The set drives the registry equality, so a matrix-declared paid label
    must reach it exactly as a directly declared one does.
    """
    source = source_over(
        tmp_path,
        {
            "ci.yml": {
                "jobs": {
                    "lint-test": {"runs-on": FORK_EXPRESSION},
                    "build": {
                        "runs-on": "${{ matrix.os }}",
                        "strategy": {
                            "matrix": {"os": ["macos-latest", "some-paid-runner"]}
                        },
                    },
                }
            }
        },
    )

    assert labels_in_use(source) == {"ubicloud-standard-2", "some-paid-runner"}


def test_matrix_keys_reads_only_matrix_references() -> None:
    """Separate a matrix reference from every other expression.

    The fork fallback is an expression too, and reading it as a matrix
    reference would send the reader looking for a matrix the job has not got.
    """
    assert matrix_keys(FORK_EXPRESSION) == frozenset()
    assert matrix_keys("${{ matrix.os }}-${{ matrix.arch }}") == frozenset(
        {"os", "arch"}
    )


def test_a_missing_workflow_directory_fails_as_a_contract_failure(
    tmp_path: pathlib.Path,
) -> None:
    """Translate an absent directory rather than raising from a comprehension.

    Every contract walks the directory. If it is not there, the suite should
    say so in those words, not surface an ``OSError`` from inside a generator.
    """
    source = WorkflowSource(tmp_path / "absent", tmp_path / "actionlint.yaml")

    with pytest.raises(UnreadableWorkflowError):
        source.names()


def test_a_non_utf8_workflow_fails_as_a_contract_failure(
    tmp_path: pathlib.Path,
) -> None:
    """Distinguish bytes that did not arrive from bytes that will not decode."""
    source = source_over(tmp_path, {})
    (source.workflow_dir / "ci.yml").write_bytes(b"name: \xff\xfe")

    with pytest.raises(UndecodableWorkflowError):
        source.document("ci.yml")


def test_an_unparsable_workflow_names_the_file(tmp_path: pathlib.Path) -> None:
    """Name the file that would not parse, since the traversal reads many."""
    source = source_over(tmp_path, {})
    (source.workflow_dir / "ci.yml").write_text("jobs: [unclosed\n", encoding="utf-8")

    with pytest.raises(UnparsableWorkflowError, match=r"ci\.yml"):
        source.document("ci.yml")


def test_a_workflow_that_is_not_a_mapping_is_refused(tmp_path: pathlib.Path) -> None:
    """Refuse a document that parses but is not a workflow."""
    source = source_over(tmp_path, {})
    (source.workflow_dir / "ci.yml").write_text("just a string\n", encoding="utf-8")

    with pytest.raises(NotAMappingError):
        source.document("ci.yml")


def test_the_registry_is_read_through_the_source(tmp_path: pathlib.Path) -> None:
    """Read the registry from the source rather than its global path.

    Otherwise the registry contract reads this repository's configuration
    whatever source it was handed, and could not be driven over a case at all.
    """
    source = source_over(
        tmp_path, {}, registry={"self-hosted-runner": {"labels": ["some-paid-runner"]}}
    )

    assert source.registered_labels() == ["some-paid-runner"]


@pytest.mark.parametrize(
    "registry",
    [
        "not a mapping",
        {"self-hosted-runner": "not a mapping"},
        {"self-hosted-runner": {}},
    ],
    ids=["document", "section", "labels list"],
)
def test_a_malformed_registry_is_refused(
    tmp_path: pathlib.Path, registry: object
) -> None:
    """Refuse a registry that is not shaped the way `actionlint` reads it.

    A registry read as an empty list would satisfy the equality only while
    no paid label is in use, and would then fail with a message about labels
    rather than about its own shape.
    """
    source = source_over(tmp_path, {}, registry=registry)

    with pytest.raises(NotAMappingError):
        source.registered_labels()
