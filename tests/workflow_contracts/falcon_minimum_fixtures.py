"""Temporary workflows and mutation cases for Falcon lower-bound contracts."""

from __future__ import annotations

import collections.abc as cabc
import typing as typ

CI_WORKFLOW: typ.Final[str] = "ci.yml"
MINIMUM_JOB: typ.Final[str] = "falcon-minimum"
NEWEST_JOB: typ.Final[str] = "lint-test"
FALCON_ENV: typ.Final[str] = "FALCON_MINIMUM_VERSION"
EXPECTED_FALCON_VERSION: typ.Final[str] = "4.0.0"
INSTALL_MARKER: typ.Final[str] = "uv pip install"
VERSION_CHECK_MARKER: typ.Final[str] = "importlib.metadata"
PYTEST_MARKER: typ.Final[str] = "pytest -v"
type WorkflowMutator = cabc.Callable[[dict[str, object]], None]


def valid_workflow() -> dict[str, object]:
    """Build the smallest CI document that satisfies this contract.

    Returns
    -------
    dict[str, object]
        A minimal workflow with both the lower-bound and newest-compatible jobs.
    """
    minimum_job: dict[str, object] = {
        "runs-on": "ubuntu-latest",
        "env": {
            FALCON_ENV: EXPECTED_FALCON_VERSION,
            "UV_PYTHON": "3.13",
        },
        "steps": [
            {"uses": "actions/checkout@v7", "with": {"persist-credentials": False}},
            {"uses": "actions/setup-python@v7", "with": {"python-version": "3.13"}},
            {"uses": "astral-sh/setup-uv@v10"},
            {"run": "make build"},
            {"run": f'{INSTALL_MARKER} "falcon==${{{FALCON_ENV}}}"'},
            {
                "run": "uv run --no-sync python -c "
                "'import importlib.metadata, os, sys; "
                'sys.exit(importlib.metadata.version("falcon") != '
                f'os.environ["{FALCON_ENV}"])\'',
            },
            {"run": f"uv run --no-sync {PYTEST_MARKER}"},
        ],
    }
    return {
        "jobs": {
            NEWEST_JOB: {"steps": [{"run": "make build"}]},
            MINIMUM_JOB: minimum_job,
        }
    }


def _mutable_mapping(value: object) -> dict[str, object]:
    """Return a mapping from the known temporary workflow fixture."""
    if not isinstance(value, dict):
        message = "temporary workflow mapping must be mutable"
        raise TypeError(message)
    return typ.cast("dict[str, object]", value)


def _mutable_steps(job: dict[str, object]) -> list[dict[str, object]]:
    """Return steps from the known temporary workflow fixture."""
    steps = job.get("steps")
    if not isinstance(steps, list):
        message = "temporary workflow steps must be mutable"
        raise TypeError(message)
    return typ.cast("list[dict[str, object]]", steps)


def _workflow_jobs(workflow: dict[str, object]) -> dict[str, object]:
    """Return the mutable jobs mapping of a temporary workflow."""
    return _mutable_mapping(workflow.get("jobs"))


def _workflow_job(workflow: dict[str, object], job_name: str) -> dict[str, object]:
    """Return a named job from a temporary workflow."""
    return _mutable_mapping(_workflow_jobs(workflow)[job_name])


def _workflow_steps(
    workflow: dict[str, object], job_name: str = MINIMUM_JOB
) -> list[dict[str, object]]:
    """Return the mutable steps of a temporary workflow job."""
    return _mutable_steps(_workflow_job(workflow, job_name))


def _workflow_environment(
    workflow: dict[str, object], job_name: str
) -> dict[str, object]:
    """Return the mutable environment of a temporary workflow job."""
    return _mutable_mapping(_workflow_job(workflow, job_name).get("env"))


def _remove_job(workflow: dict[str, object], job_name: str) -> None:
    """Remove a named job from a temporary workflow."""
    del _workflow_jobs(workflow)[job_name]


def _set_job_value(
    workflow: dict[str, object], job_name: str, name: str, value: object
) -> None:
    """Set one field on a temporary workflow job."""
    _workflow_job(workflow, job_name)[name] = value


def _set_environment_value(
    workflow: dict[str, object], job_name: str, name: str, value: object
) -> None:
    """Set one environment value on a temporary workflow job."""
    _workflow_environment(workflow, job_name)[name] = value


def _remove_environment_value(
    workflow: dict[str, object], job_name: str, name: str
) -> None:
    """Remove one environment value from a temporary workflow job."""
    del _workflow_environment(workflow, job_name)[name]


def _step_index(workflow: dict[str, object], marker: str) -> int:
    """Return the only minimum-job step whose run command contains a marker."""
    matches = []
    for index, step in enumerate(_workflow_steps(workflow)):
        run = step.get("run")
        if isinstance(run, str) and marker in run:
            matches.append(index)
    if len(matches) != 1:
        message = (
            f"expected one minimum-job step containing {marker!r}; found {len(matches)}"
        )
        raise ValueError(message)
    return matches[0]


def _remove_step(workflow: dict[str, object], marker: str) -> None:
    """Remove the unique minimum-job step containing a command marker."""
    del _workflow_steps(workflow)[_step_index(workflow, marker)]


def _set_step_value(
    workflow: dict[str, object], marker: str, name: str, value: object
) -> None:
    """Set one field on the unique minimum-job step containing a marker."""
    _workflow_steps(workflow)[_step_index(workflow, marker)][name] = value


def _set_step_command(workflow: dict[str, object], marker: str, command: str) -> None:
    """Replace the run command of the unique minimum-job step containing a marker."""
    _workflow_steps(workflow)[_step_index(workflow, marker)]["run"] = command


def _command_step(
    workflow: dict[str, object], marker: str
) -> tuple[dict[str, object], str]:
    """Find a unique step and return its checked command for rewriting."""
    step = _workflow_steps(workflow)[_step_index(workflow, marker)]
    run = step.get("run")
    if not isinstance(run, str):
        message = "temporary step command must be a string"
        raise TypeError(message)
    return step, run


def _append_to_step_command(
    workflow: dict[str, object], marker: str, suffix: str
) -> None:
    """Append shell syntax to the unique minimum-job step containing a marker."""
    step, run = _command_step(workflow, marker)
    step["run"] = f"{run}{suffix}"


def _drop_sync_from_version_check(workflow: dict[str, object]) -> None:
    """Remove the no-sync flag from the version-check command."""
    step, run = _command_step(workflow, VERSION_CHECK_MARKER)
    step["run"] = run.replace(" --no-sync", "")


def _check_wrong_package(workflow: dict[str, object]) -> None:
    """Point the version check at a package other than Falcon."""
    step, run = _command_step(workflow, VERSION_CHECK_MARKER)
    changed_run = run.replace('version("falcon")', 'version("msgspec")')
    if changed_run == run:
        message = "temporary version-check command must query Falcon"
        raise ValueError(message)
    step["run"] = changed_run


def _append_step(workflow: dict[str, object], command: str) -> None:
    """Append a command step to the minimum-version job."""
    _workflow_steps(workflow).append({"run": command})


def _replace_newest_steps(workflow: dict[str, object]) -> None:
    """Replace the newest-compatible job steps with an incomplete command."""
    _workflow_job(workflow, NEWEST_JOB)["steps"] = [{"run": "uv run pytest -v"}]


REGRESSION_MUTATIONS: typ.Final[dict[str, WorkflowMutator]] = {
    "job-removed": lambda workflow: _remove_job(workflow, MINIMUM_JOB),
    "pin-changed-to-4.4.0": lambda workflow: _set_environment_value(
        workflow, MINIMUM_JOB, FALCON_ENV, "4.4.0"
    ),
    "pin-removed": lambda workflow: _remove_environment_value(
        workflow, MINIMUM_JOB, FALCON_ENV
    ),
    "install-step-removed": lambda workflow: _remove_step(workflow, INSTALL_MARKER),
    "verification-step-removed": lambda workflow: _remove_step(
        workflow, VERSION_CHECK_MARKER
    ),
    "no-sync-dropped-from-pytest": lambda workflow: _set_step_command(
        workflow, PYTEST_MARKER, "uv run pytest -v"
    ),
    "pytest-filter-added": lambda workflow: _set_step_command(
        workflow, PYTEST_MARKER, "uv run --no-sync pytest -v tests"
    ),
    "version-check-no-sync-dropped": _drop_sync_from_version_check,
    "version-check-wrong-package": _check_wrong_package,
    "version-check-failure-masked": lambda workflow: _append_to_step_command(
        workflow, VERSION_CHECK_MARKER, " || true"
    ),
    "pytest-empty-single-quote-hash": lambda workflow: _set_step_command(
        workflow, PYTEST_MARKER, "uv run --no-sync pytest -v ''# || true"
    ),
    "pytest-empty-double-quote-hash": lambda workflow: _set_step_command(
        workflow, PYTEST_MARKER, 'uv run --no-sync pytest -v ""# || true'
    ),
    "pytest-hash-masks-failure": lambda workflow: _set_step_command(
        workflow, PYTEST_MARKER, "uv run --no-sync pytest -v# || true"
    ),
    "pytest-pipeline-skipped-by-or": lambda workflow: _set_step_command(
        workflow,
        PYTEST_MARKER,
        "true || printf unused | uv run --no-sync pytest -v",
    ),
    "pytest-pipeline-skipped-by-and": lambda workflow: _set_step_command(
        workflow,
        PYTEST_MARKER,
        "false && printf unused | uv run --no-sync pytest -v",
    ),
    "pytest-skipped-by-and": lambda workflow: _set_step_command(
        workflow, PYTEST_MARKER, "false && uv run --no-sync pytest -v"
    ),
    "pytest-skipped-by-or": lambda workflow: _set_step_command(
        workflow, PYTEST_MARKER, "true || uv run --no-sync pytest -v"
    ),
    "pytest-failure-masked": lambda workflow: _append_to_step_command(
        workflow, PYTEST_MARKER, " || true"
    ),
    "make-test-after-override": lambda workflow: _append_step(workflow, "make test"),
    "uv-sync-after-override": lambda workflow: _append_step(
        workflow, "uv sync --group dev --locked"
    ),
    "syncing-uv-run-after-override": lambda workflow: _append_step(
        workflow, "uv run pytest -v"
    ),
    "falcon-reinstalled-after-override": lambda workflow: _append_step(
        workflow, "uv pip install falcon==4.4.0"
    ),
    "make-test-through-no-sync-uv-run": lambda workflow: _append_step(
        workflow, "uv run --no-sync make test"
    ),
    "continue-on-error-true": lambda workflow: _set_job_value(
        workflow, MINIMUM_JOB, "continue-on-error", value=True
    ),
    "step-continue-on-error-true": lambda workflow: _set_step_value(
        workflow, PYTEST_MARKER, "continue-on-error", value=True
    ),
    "step-if-skips-pytest": lambda workflow: _set_step_value(
        workflow, PYTEST_MARKER, "if", "github.event_name == 'push'"
    ),
    "job-if-limits-triggers": lambda workflow: _set_job_value(
        workflow, MINIMUM_JOB, "if", "github.event_name == 'push'"
    ),
    "lint-test-removed": lambda workflow: _remove_job(workflow, NEWEST_JOB),
    "falcon-override-added-to-lint-test": lambda workflow: _set_job_value(
        workflow,
        NEWEST_JOB,
        "env",
        {FALCON_ENV: EXPECTED_FALCON_VERSION},
    ),
    "make-build-removed-from-lint-test": _replace_newest_steps,
}
