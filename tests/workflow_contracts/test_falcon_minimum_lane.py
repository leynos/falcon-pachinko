"""Keep the CI lane that exercises Falcon's declared lower bound.

The lockfile deliberately selects the newest compatible Falcon release for
the main CI lane. This contract keeps a separate lane on the package's lower
bound and proves its override survives the version check and test run.
"""

from __future__ import annotations

import collections.abc as cabc
import functools
import shlex
import tomllib
import typing as typ

import pytest
import yaml
from packaging.requirements import Requirement
from packaging.version import Version

from .falcon_minimum_fixtures import (
    CI_WORKFLOW,
    EXPECTED_FALCON_VERSION,
    FALCON_ENV,
    MINIMUM_JOB,
    NEWEST_JOB,
    REGRESSION_MUTATIONS,
    valid_workflow,
)
from .workflow_support import (
    REPOSITORY,
    ROOT,
    WorkflowSource,
    as_mapping,
)

if typ.TYPE_CHECKING:
    import pathlib

type CommandGroup = tuple[str, ...]
type CommandLine = tuple[CommandGroup, str | None]
type CommandEntry = tuple[int, int, CommandGroup, str | None]
type CommandPredicate = cabc.Callable[[CommandGroup], bool]


@functools.cache
def _project_falcon_lower_bound() -> str:
    """Read and normalize the Falcon lower bound from project metadata."""
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))[
        "project"
    ]
    requirements = [Requirement(dependency) for dependency in project["dependencies"]]
    requirement = next(
        (parsed for parsed in requirements if parsed.name == "falcon"),
        None,
    )
    if requirement is None:
        message = "pyproject.toml must declare a Falcon dependency"
        raise ValueError(message)
    lower_bounds = [
        Version(bound.version)
        for bound in requirement.specifier
        if bound.operator in {">", ">="}
    ]
    if not lower_bounds:
        message = "Falcon dependency has no lower bound"
        raise ValueError(message)
    release = max(lower_bounds).release
    padded_release = (*release, 0, 0)[:3]
    return ".".join(str(component) for component in padded_release)


def _command_groups(run: str) -> list[CommandLine]:
    """Split a run script into commands and retain their following operators."""
    groups: list[CommandLine] = []
    continued = run.replace("\\\n", " ")
    for line in continued.splitlines():
        lexer = shlex.shlex(line, posix=True, punctuation_chars=";&|")
        lexer.whitespace_split = True
        tokens = list(lexer)
        current: list[str] = []
        for token in tokens:
            if token and all(character in ";&|" for character in token):
                if current:
                    groups.append((tuple(current), token))
                    current = []
            elif token in {"then", "do"} and not current:
                continue
            else:
                current.append(token)
        if current:
            groups.append((tuple(current), None))
    return groups


def _steps(
    job: dict[str, object], subject: str, violations: list[str]
) -> list[dict[str, object]]:
    """Return the job's steps, retaining malformed positions for diagnostics."""
    raw_steps = job.get("steps")
    if not isinstance(raw_steps, list):
        violations.append(f"{subject} must declare steps")
        return []
    steps: list[dict[str, object]] = []
    for index, raw_step in enumerate(raw_steps):
        step = as_mapping(raw_step)
        if step is None:
            violations.append(f"{subject} step {index} must be a mapping")
            steps.append({})
        else:
            steps.append(step)
    return steps


def _commands(
    steps: list[dict[str, object]],
) -> list[CommandEntry]:
    """Return run commands with their step and within-step positions."""
    found: list[CommandEntry] = []
    for step_index, step in enumerate(steps):
        run = step.get("run")
        if not isinstance(run, str):
            continue
        for command_index, (command, separator) in enumerate(_command_groups(run)):
            found.append((step_index, command_index, command, separator))
    return found


def _is_command(command: CommandGroup, *arguments: str) -> bool:
    """Report whether a command begins with the requested argument sequence."""
    return command[: len(arguments)] == arguments


def _install_command(command: CommandGroup) -> bool:
    """Report whether a command installs Falcon from the lane's environment pin."""
    if not _is_command(command, "uv", "pip", "install"):
        return False
    rendered = " ".join(command[3:])
    return "falcon==" in rendered and FALCON_ENV in rendered


def _version_check_command(command: CommandGroup) -> bool:
    """Report whether a no-sync Python command checks installed Falcon."""
    if not _is_command(command, "uv", "run", "--no-sync", "python", "-c"):
        return False
    code = " ".join(command[5:])
    return (
        "importlib.metadata.version" in code
        and ("version('falcon')" in code or 'version("falcon")' in code)
        and FALCON_ENV in code
        and ("==" in code or "!=" in code)
    )


def _pytest_command(command: CommandGroup) -> bool:
    """Report whether a command runs the full suite without syncing the environment."""
    return command == ("uv", "run", "--no-sync", "pytest", "-v")


def _unsafe_environment_command(command: CommandGroup) -> bool:
    """Report a command that can replace the Falcon override with the lockfile."""
    if command and command[0] == "make":
        return True
    if _is_command(command, "uv", "pip", "install"):
        return any("falcon==" in argument.casefold() for argument in command[3:])
    if not _is_command(command, "uv") or len(command) < 2:
        return False
    return command[1] == "sync" or (
        command[1] == "run" and ("--no-sync" not in command or "make" in command[2:])
    )


def _is_enabled(value: object) -> bool:
    """Recognize YAML booleans and quoted boolean strings consistently."""
    return value is True or (isinstance(value, str) and value.casefold() == "true")


def _falcon_override(job: dict[str, object], steps: list[dict[str, object]]) -> bool:
    """Report any Falcon-specific environment pin or package installation."""
    environments = [as_mapping(job.get("env"))]
    environments.extend(as_mapping(step.get("env")) for step in steps)
    if any(
        "falcon" in key.casefold()
        for environment in environments
        if environment is not None
        for key in environment
    ):
        return True
    return any(
        "falcon==" in " ".join(command).casefold()
        for _, _, command, _ in _commands(steps)
    )


def _job_condition_violations(job: dict[str, object]) -> list[str]:
    """Refuse lane-level conditions that can skip coverage of the CI triggers."""
    violations = []
    if "if" in job:
        violations.append(f"{MINIMUM_JOB} must not have a job-level if condition")
    if _is_enabled(job.get("continue-on-error")):
        violations.append(f"{MINIMUM_JOB} must not continue on error")
    return violations


def _environment_violations(job: dict[str, object]) -> list[str]:
    """Hold the workflow pins to Falcon's project lower bound and Python 3.13."""
    environment = as_mapping(job.get("env"))
    expected = (
        (
            FALCON_ENV,
            EXPECTED_FALCON_VERSION,
            f"{MINIMUM_JOB}.env.{FALCON_ENV} must be {EXPECTED_FALCON_VERSION!r}",
        ),
        ("UV_PYTHON", "3.13", f"{MINIMUM_JOB}.env.UV_PYTHON must be '3.13'"),
    )
    violations = [
        message
        for name, value, message in expected
        if environment is None or environment.get(name) != value
    ]
    lower_bound = _project_falcon_lower_bound()
    if environment is None or environment.get(FALCON_ENV) != lower_bound:
        violations.append(
            f"{MINIMUM_JOB}.env.{FALCON_ENV} must match the Falcon lower bound "
            f"{lower_bound!r} from pyproject.toml"
        )
    return violations


def _step_condition_violations(steps: list[dict[str, object]]) -> list[str]:
    """Refuse steps that can fail or be skipped without failing the job."""
    violations = []
    for index, step in enumerate(steps):
        if _is_enabled(step.get("continue-on-error")):
            violations.append(f"{MINIMUM_JOB} step {index} must not continue on error")
        if "if" in step:
            violations.append(
                f"{MINIMUM_JOB} step {index} must not have an if condition"
            )
    return violations


def _action_index(steps: list[dict[str, object]], prefix: str) -> int | None:
    """Find the first setup step whose action uses *prefix*."""
    for index, step in enumerate(steps):
        uses = step.get("uses")
        if isinstance(uses, str) and uses.startswith(prefix):
            return index
    return None


def _setup_positions(
    steps: list[dict[str, object]],
) -> tuple[dict[str, int | None], list[str]]:
    """Find setup steps and report missing checkout or tool installations."""
    positions = {
        "checkout": _action_index(steps, "actions/checkout@"),
        "python": _action_index(steps, "actions/setup-python@"),
        "uv": _action_index(steps, "astral-sh/setup-uv@"),
    }
    violations = []
    if positions["checkout"] is None:
        violations.append(f"{MINIMUM_JOB} must check out the repository")
    if positions["uv"] is None:
        violations.append(f"{MINIMUM_JOB} must set up uv")
    python_index = positions["python"]
    python_step = steps[python_index] if python_index is not None else {}
    python_inputs = as_mapping(python_step.get("with"))
    if python_inputs is None or python_inputs.get("python-version") != "3.13":
        violations.append(f"{MINIMUM_JOB} must set up Python 3.13")
    return positions, violations


def _first_command(
    commands: list[CommandEntry], predicate: CommandPredicate
) -> CommandEntry | None:
    """Return the first command accepted by *predicate*."""
    return next((entry for entry in commands if predicate(entry[2])), None)


def _command_step_index(
    commands: dict[str, CommandEntry | None], name: str
) -> int | None:
    """Return a required command's step index when the command was found."""
    command = commands[name]
    return command[0] if command is not None else None


def _required_commands(
    commands: list[CommandEntry],
) -> tuple[dict[str, CommandEntry | None], list[str]]:
    """Find the build, Falcon install, version check, and complete test run."""
    found = {
        "build": _first_command(
            commands, lambda command: _is_command(command, "make", "build")
        ),
        "install": _first_command(commands, _install_command),
        "verify": _first_command(commands, _version_check_command),
        "pytest": _first_command(commands, _pytest_command),
    }
    descriptions = (
        ("build", "run make build"),
        ("install", f"install falcon==${{{FALCON_ENV}}}"),
        ("verify", "verify Falcon with uv run --no-sync"),
        ("pytest", "run pytest with uv run --no-sync"),
    )
    violations = [
        f"{MINIMUM_JOB} must {description}"
        for name, description in descriptions
        if found[name] is None
    ]
    return found, violations


def _masked_command_violations(
    required_commands: dict[str, CommandEntry | None],
) -> list[str]:
    """Reject required commands followed by operators that can mask failure."""
    return [
        (
            f"{MINIMUM_JOB} {name} command must not be followed by "
            f"failure-masking shell operator {entry[3]!r}"
        )
        for name, entry in required_commands.items()
        if entry is not None and entry[3] in {"||", "|", "&"}
    ]


def _step_order_violations(
    setup_positions: dict[str, int | None],
    required_commands: dict[str, CommandEntry | None],
) -> list[str]:
    """Keep checkout, setup, build, install, check, and tests in separate steps."""
    positions = [
        setup_positions["checkout"],
        setup_positions["python"],
        setup_positions["uv"],
        *(
            _command_step_index(required_commands, name)
            for name in ("build", "install", "verify", "pytest")
        ),
    ]
    if any(position is None for position in positions):
        return []
    ordered = [position for position in positions if position is not None]
    if ordered != sorted(set(ordered)):
        return [
            (
                f"{MINIMUM_JOB} must order checkout, Python, uv, build, install, "
                "version check, and pytest as separate steps"
            )
        ]
    return []


def _resync_violations(
    commands: list[CommandEntry],
    required_commands: dict[str, CommandEntry | None],
) -> list[str]:
    """Refuse later commands that can replace the minimum-version override."""
    install = required_commands["install"]
    if install is None:
        return []
    install_step, install_position, _, _ = install
    unsafe = [
        command
        for step_index, command_index, command, _ in commands
        if (
            step_index > install_step
            or (step_index == install_step and command_index > install_position)
        )
        and _unsafe_environment_command(command)
    ]
    if unsafe:
        return [
            (
                f"{MINIMUM_JOB} must not run make, uv sync, or syncing uv run "
                f"after the Falcon override: {unsafe!r}"
            )
        ]
    return []


def _minimum_job_violations(job: dict[str, object]) -> list[str]:
    """Inspect the environment and ordered commands of the lower-bound job."""
    violations = _job_condition_violations(job)
    violations.extend(_environment_violations(job))
    steps = _steps(job, MINIMUM_JOB, violations)
    violations.extend(_step_condition_violations(steps))
    setup_positions, setup_violations = _setup_positions(steps)
    violations.extend(setup_violations)
    commands = _commands(steps)
    required_commands, command_violations = _required_commands(commands)
    violations.extend(command_violations)
    violations.extend(_step_order_violations(setup_positions, required_commands))
    violations.extend(_masked_command_violations(required_commands))
    violations.extend(_resync_violations(commands, required_commands))
    return violations


def _newest_job_violations(job: dict[str, object]) -> list[str]:
    """Keep the locked-version lane's build and dependency resolution intact."""
    violations = []
    steps = _steps(job, NEWEST_JOB, violations)
    commands = _commands(steps)
    if not any(_is_command(command, "make", "build") for _, _, command, _ in commands):
        violations.append(f"{NEWEST_JOB} must continue to run make build")
    if _falcon_override(job, steps):
        violations.append(f"{NEWEST_JOB} must not override the locked Falcon release")
    return violations


def falcon_minimum_lane_violations(source: WorkflowSource) -> list[str]:
    """Return every violation in the minimum-version and newest lanes.

    Parameters
    ----------
    source : WorkflowSource
        The workflow directory to inspect.

    Returns
    -------
    list[str]
        One message per violation; empty when the contract holds.
    """
    jobs = source.jobs(CI_WORKFLOW)
    violations = []
    minimum_job = jobs.get(MINIMUM_JOB)
    if minimum_job is None:
        violations.append(f"{CI_WORKFLOW} must declare the {MINIMUM_JOB} job")
    else:
        violations.extend(_minimum_job_violations(minimum_job))

    newest_job = jobs.get(NEWEST_JOB)
    if newest_job is None:
        violations.append(f"{CI_WORKFLOW} must retain the {NEWEST_JOB} job")
    else:
        violations.extend(_newest_job_violations(newest_job))
    return violations


def _temporary_workflow_source(
    directory: pathlib.Path, workflow: dict[str, object]
) -> WorkflowSource:
    """Write a temporary CI workflow and return its source reader."""
    workflow_dir = directory / "workflows"
    workflow_dir.mkdir()
    (workflow_dir / CI_WORKFLOW).write_text(yaml.safe_dump(workflow), encoding="utf-8")
    return WorkflowSource(workflow_dir, directory / "actionlint.yaml")


def test_the_repository_lane_matches_the_declared_lower_bound() -> None:
    """The checked-in workflow exercises the current Falcon lower bound."""
    violations = falcon_minimum_lane_violations(REPOSITORY)

    assert not violations, "\n".join(violations)


@pytest.mark.parametrize(
    "case",
    tuple(REGRESSION_MUTATIONS),
    ids=tuple(REGRESSION_MUTATIONS),
)
def test_a_minimum_lane_regression_is_reported(
    tmp_path: pathlib.Path, case: str
) -> None:
    """The contract rejects each named regression in a temporary workflow."""
    workflow = valid_workflow()
    REGRESSION_MUTATIONS[case](workflow)
    source = _temporary_workflow_source(tmp_path, workflow)

    assert falcon_minimum_lane_violations(source), f"{case} was accepted"


def test_a_minimal_valid_minimum_lane_has_no_violations(
    tmp_path: pathlib.Path,
) -> None:
    """A small but effective lower-bound lane satisfies the inspection rule."""
    source = _temporary_workflow_source(tmp_path, valid_workflow())

    assert not falcon_minimum_lane_violations(source), (
        "a valid temporary lower-bound lane must satisfy the contract"
    )
