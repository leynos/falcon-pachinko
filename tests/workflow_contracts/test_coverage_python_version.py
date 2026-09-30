"""Contract: every coverage call measures on one declared Python the project accepts.

generate-coverage chooses its interpreter in a fixed order: its own
``python-version`` input, then ``UV_PYTHON``, then the first entry of
``.python-version``, then the ``python3`` the job put on ``PATH``, which is the
most recent ``actions/setup-python`` step before the call in its job. If the
chosen version is outside ``requires-python``, ``uv sync`` refuses it and the
coverage step fails.

So every call in the pull-request lane and the publisher must declare at least
one of those sources, every source it declares must name the same version (a
higher-priority value silently overriding a lower one is how lanes drift), and
that version must be inside ``requires-python``. A setup step guarded by
``if:`` or allowed to fail with ``continue-on-error`` may not run, so it
declares nothing. The ratchet baseline key already carries the interpreter
(``ratchet-baseline-<os>-py<major.minor>-``), so a lane on another Python
misses its baseline rather than comparing against the wrong one; the lane
parity check here makes that miss a contract failure instead of a silent
restart.
"""

from __future__ import annotations

import itertools
import typing as typ

import pytest
import yaml
from packaging.specifiers import SpecifierSet

from .coverage_python_sources import (
    LANES,
    ROOT,
    SETUP_PYTHON,
    WORKFLOWS,
    CoverageCall,
    coverage_calls,
    python_version_entry,
    read_required_text,
    read_text_if_present,
    rejected_versions,
    requires_python,
    verdict,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    from pathlib import Path

#: Minimal steps for the fixture workflows the selection tests build.
SETUP: typ.Final[dict[str, object]] = {"uses": f"{SETUP_PYTHON}{'0' * 40}"}
COVERAGE: typ.Final[dict[str, object]] = {
    "uses": f"leynos/shared-actions/.github/actions/generate-coverage@{'0' * 40}"
}
AGREE: typ.Final[str] = "3.14"
CONFLICT: typ.Final[str] = "3.13"


def _lane_calls() -> dict[str, list[CoverageCall]]:
    """Return both lanes' coverage calls, keyed by workflow file name."""
    python_version = python_version_entry(
        read_text_if_present(ROOT / ".python-version")
    )
    return {
        lane: coverage_calls(read_required_text(WORKFLOWS / lane), python_version)
        for lane in LANES
    }


def test_both_lanes_call_generate_coverage() -> None:
    """The pull-request lane and the publisher each measure coverage."""
    assert all(_lane_calls().values()), f"{LANES} must each call generate-coverage"


def test_every_call_declares_one_accepted_python() -> None:
    """Each call names a Python, every source agrees, and the project accepts it."""
    accepted = requires_python(read_required_text(ROOT / "pyproject.toml"))
    for lane, calls in _lane_calls().items():
        for call in calls:
            where = f"{lane}:{call.job}"
            assert not verdict(call), f"{where} is {verdict(call)}: {call.sources}"
            assert rejected_versions(accepted, [call.effective]) == [], (
                f"{where} measures on {call.effective}, outside {accepted}"
            )


def test_both_lanes_measure_on_one_python() -> None:
    """Both lanes measure on one Python, as the pull-request ratchet assumes."""
    effective = {call.effective for calls in _lane_calls().values() for call in calls}

    assert len(effective) == 1, f"coverage lanes measure on {sorted(effective)}"


def _workflow(
    jobs: cabc.Mapping[str, object], env: dict[str, str] | None = None
) -> str:
    """Render a workflow with the given jobs, keeping their order."""
    document: dict[str, object] = {"jobs": dict(jobs)}
    if env:
        document["env"] = env
    return yaml.safe_dump(document, sort_keys=False)


def _setup(version: str, **extra: object) -> dict[str, object]:
    """Return a setup-python step requesting ``version``."""
    return {**SETUP, "with": {"python-version": version}, **extra}


def _steps(*steps: dict[str, object]) -> dict[str, object]:
    """Return a job with the given steps."""
    return {"steps": list(steps)}


@pytest.mark.parametrize(
    ("jobs", "expected"),
    [
        ({"cov": _steps(_setup("3.14"), COVERAGE)}, ["3.14"]),
        ({"cov": _steps(COVERAGE, _setup("3.14"))}, [""]),
        ({"other": _steps(_setup("3.14")), "cov": _steps(COVERAGE)}, [""]),
        ({"lint": _steps(_setup("3.14"))}, []),
        (
            {"cov": _steps(_setup("3.13"), COVERAGE, _setup("3.14"), COVERAGE)},
            ["3.13", "3.14"],
        ),
    ],
    ids=[
        "before-in-job",
        "after-the-step",
        "another-job",
        "no-coverage-job",
        "latest-setup-per-call",
    ],
)
def test_each_call_reads_the_latest_setup_before_it_in_its_job(
    jobs: dict[str, object], expected: list[str]
) -> None:
    """A call's setup-python source is its own job's latest setup before it."""
    calls = coverage_calls(_workflow(jobs))

    assert [call.sources["setup-python"] for call in calls] == expected


@pytest.mark.parametrize(
    ("step_env", "job_env", "workflow_env", "expected"),
    [
        ({"UV_PYTHON": "3.12"}, {"UV_PYTHON": "3.13"}, {"UV_PYTHON": "3.14"}, "3.12"),
        ({}, {"UV_PYTHON": "3.13"}, {"UV_PYTHON": "3.14"}, "3.13"),
    ],
    ids=["step-over-job-and-workflow", "job-over-workflow"],
)
def test_the_innermost_uv_python_is_read(
    step_env: dict[str, str],
    job_env: dict[str, str],
    workflow_env: dict[str, str],
    expected: str,
) -> None:
    """``UV_PYTHON`` set in several scopes resolves to the innermost one."""
    call = {**COVERAGE, **({"env": step_env} if step_env else {})}
    job = {**_steps(call), "env": job_env}
    (read,) = coverage_calls(_workflow({"cov": job}, workflow_env))

    assert read.sources["UV_PYTHON"] == expected


def test_an_empty_step_uv_python_wins_over_the_outer_value() -> None:
    """An empty step ``UV_PYTHON`` replaces the job's, so there is no false conflict.

    The action then falls through to ``.python-version``, which agrees with
    the setup step; reading the job's ``3.13`` instead would report a conflict.
    """
    step = {**COVERAGE, "env": {"UV_PYTHON": ""}}
    job = {**_steps(_setup(AGREE), step), "env": {"UV_PYTHON": CONFLICT}}
    (call,) = coverage_calls(_workflow({"cov": job}), AGREE)

    assert call.sources["UV_PYTHON"] == "", "the empty step value must win"
    assert verdict(call) == "", f"no conflict expected, got {call.sources}"


def test_an_absent_step_uv_python_still_inherits_the_job_value() -> None:
    """Narrow: with no step value the job's ``UV_PYTHON`` applies, and conflicts."""
    job = {**_steps(_setup(AGREE), COVERAGE), "env": {"UV_PYTHON": CONFLICT}}
    (call,) = coverage_calls(_workflow({"cov": job}), AGREE)

    assert verdict(call) == "conflicting", f"expected a conflict, got {call.sources}"


@pytest.mark.parametrize(
    "guard",
    [{"if": "always()"}, {"continue-on-error": True}],
    ids=["guarded", "continue-on-error"],
)
def test_a_guarded_setup_after_a_reliable_one_declares_nothing(
    guard: dict[str, object],
) -> None:
    """A later setup that may not run leaves nothing the call can rely on.

    The earlier setup's Python may or may not still be first on ``PATH`` once a
    later one can replace it, so with no other source the call is undeclared.
    """
    job = _steps(_setup(AGREE), _setup(CONFLICT, **guard), COVERAGE)
    (call,) = coverage_calls(_workflow({"cov": job}))

    assert call.sources["setup-python"] == "", f"setup read as {call.sources}"
    assert verdict(call) == "undeclared", f"expected undeclared, got {call.sources}"


def test_an_empty_job_uv_python_wins_over_the_workflow_value() -> None:
    """An empty job ``UV_PYTHON`` replaces the workflow's, so there is no conflict."""
    job = {**_steps(_setup(AGREE), COVERAGE), "env": {"UV_PYTHON": ""}}
    (call,) = coverage_calls(_workflow({"cov": job}, {"UV_PYTHON": CONFLICT}), AGREE)

    assert call.sources["UV_PYTHON"] == "", "the empty job value must win"
    assert verdict(call) == "", f"no conflict expected, got {call.sources}"


@pytest.mark.parametrize(
    "workflow",
    [
        "jobs:\n  cov:\n    steps: []\n    steps: []\n",
        "jobs:\n  cov:\n    steps: []\njobs: {}\n",
    ],
    ids=["duplicate-steps", "duplicate-jobs"],
)
def test_a_workflow_repeating_a_key_is_refused(workflow: str) -> None:
    """A repeated mapping key cannot hide a coverage step or a job."""
    with pytest.raises(yaml.YAMLError):
        coverage_calls(workflow)


class SourceCombination(typ.NamedTuple):
    """One combination of the sources the resolver reads for a single call."""

    input: str
    uv_scope: str
    uv_version: str
    python_version: str
    setup: str

    def render(self) -> str:
        """Return the fixture workflow declaring exactly these sources."""
        setup = {
            "named": _setup(AGREE),
            "unversioned": dict(SETUP),
            "if": _setup(AGREE, **{"if": "false"}),
            "continue-on-error": _setup(AGREE, **{"continue-on-error": True}),
        }[self.setup]
        call = dict(COVERAGE)
        if self.input:
            call["with"] = {"python-version": self.input}
        uv = {"UV_PYTHON": self.uv_version}
        if self.uv_scope == "step":
            call["env"] = uv
        job = {**_steps(setup, call), **({"env": uv} if self.uv_scope == "job" else {})}
        return _workflow({"cov": job}, uv if self.uv_scope == "workflow" else None)

    def expected_declared(self) -> dict[str, str]:
        """Return the sources this combination declares, highest priority first."""
        named = {
            "input": self.input,
            "UV_PYTHON": self.uv_version if self.uv_scope else "",
            ".python-version": self.python_version,
            "setup-python": AGREE if self.setup == "named" else "",
        }
        return {name: version for name, version in named.items() if version}


_UV = [("", "")] + [
    (scope, version)
    for scope in ("step", "job", "workflow")
    for version in (AGREE, CONFLICT)
]
COMBINATIONS = [
    SourceCombination(given, uv_scope, uv_version, python_version, setup)
    for given, (uv_scope, uv_version), python_version, setup in itertools.product(
        ("", AGREE, CONFLICT),
        _UV,
        ("", AGREE, CONFLICT),
        ("named", "unversioned", "if", "continue-on-error"),
    )
]


@pytest.mark.parametrize("combination", COMBINATIONS, ids=str)
def test_every_source_combination_is_read_and_judged(
    combination: SourceCombination,
) -> None:
    """Exhaustively: each declared source is read, and disagreement or absence fails."""
    (call,) = coverage_calls(combination.render(), combination.python_version)
    declared = combination.expected_declared()
    versions = set(declared.values())

    assert call.declared == declared
    assert call.effective == next(iter(declared.values()), "")
    assert verdict(call) == (
        "undeclared" if not versions else "conflicting" if len(versions) > 1 else ""
    )


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (None, ""),
        ("", ""),
        ("3.13\n", "3.13"),
        ("# pinned\n\n  3.13  \n3.12\n", "3.13"),
        ("# comments only\n", ""),
    ],
    ids=["absent", "empty", "one-entry", "first-entry-after-comments", "comments-only"],
)
def test_the_python_version_entry_is_the_first_non_comment_line(
    text: str | None, expected: str
) -> None:
    """Parsing is pure: the first non-comment entry, or nothing."""
    assert python_version_entry(text) == expected


def test_a_python_version_file_is_read_from_the_tree(tmp_path: Path) -> None:
    """A real ``.python-version`` feeds the parser; a missing one reads as absent."""
    present = tmp_path / ".python-version"
    present.write_text("# pinned\n3.12\n", encoding="utf-8")

    assert python_version_entry(read_text_if_present(present)) == "3.12"
    assert read_text_if_present(tmp_path / "missing" / ".python-version") is None


def test_an_optional_file_that_cannot_be_read_fails_loudly(tmp_path: Path) -> None:
    """Only absence reads as absent; a directory or undecodable file raises."""
    undecodable = tmp_path / "bytes"
    undecodable.write_bytes(b"\xff\xfe")
    directory = tmp_path / "directory"
    directory.mkdir()

    with pytest.raises(UnicodeDecodeError):
        read_text_if_present(undecodable)
    with pytest.raises(OSError, match="directory"):
        read_text_if_present(directory)


def test_a_required_file_that_is_missing_fails_loudly(tmp_path: Path) -> None:
    """A file the contract needs raises rather than reading as absent."""
    with pytest.raises(FileNotFoundError):
        read_required_text(tmp_path / "missing.yml")


@pytest.mark.parametrize(
    ("specifier", "requested", "rejected"),
    [
        (">=3.14", ["3.14"], []),
        (">=3.14", ["3.15"], []),
        (">=3.14", ["3.13"], ["3.13"]),
        (">=3.12,<3.14", ["3.14", "3.12"], ["3.14"]),
    ],
)
def test_the_check_rejects_exactly_the_versions_outside_the_range(
    specifier: str, requested: list[str], rejected: list[str]
) -> None:
    """The comparison is by version, in both directions of the range."""
    assert rejected_versions(SpecifierSet(specifier), requested) == rejected
