"""Protect Falcon's lower-bound lane and the locked-version CI lane."""

from __future__ import annotations

import ast
import collections.abc as cabc
import shlex
import tomllib
import typing as typ

import pytest
import yaml
from hypothesis import given
from hypothesis import strategies as st
from packaging.requirements import Requirement
from packaging.version import Version

from .falcon_minimum_fixtures import (
    CI_WORKFLOW,
    EXPECTED_FALCON_VERSION,
    EXPECTED_REGRESSION_VIOLATIONS,
    FALCON_ENV,
    MINIMUM_JOB,
    NEWEST_JOB,
    REGRESSION_MUTATIONS,
    SHELL_STATE_CASES,
    VERSION_CHECK_PROGRAMS,
    _command_step,
    valid_workflow,
)
from .workflow_support import (
    REPOSITORY,
    ROOT,
    WorkflowShapeError,
    WorkflowSource,
    as_mapping,
)

if typ.TYPE_CHECKING:
    import pathlib

type CommandGroup = tuple[str, ...]
type CommandLine = tuple[CommandGroup, str | None]
type CommandEntry = tuple[int, int, CommandGroup, str | None]
type CommandPredicate = cabc.Callable[[CommandGroup], bool]


def _project_falcon_lower_bound(path: pathlib.Path) -> str:
    """Read and normalize the Falcon lower bound from project metadata."""
    project = tomllib.loads(WorkflowSource.text(path))["project"]
    requirements = [Requirement(dependency) for dependency in project["dependencies"]]
    requirement = next(
        (parsed for parsed in requirements if parsed.name == "falcon"),
        None,
    )
    if requirement is None:
        message = "pyproject.toml must declare a Falcon dependency"
        raise ValueError(message)
    return _normalise_falcon_lower_bound(requirement)


def _normalise_falcon_lower_bound(requirement: Requirement) -> str:
    """Select the strongest lower bound and pad its release to three parts."""
    lower_bounds = [
        Version(bound.version)
        for bound in requirement.specifier
        if bound.operator in {">", ">="}
    ]
    if not lower_bounds:
        message = "Falcon dependency has no lower bound"
        raise ValueError(message)
    return ".".join(
        str(component) for component in (*max(lower_bounds).release, 0, 0)[:3]
    )


def _command_groups(run: str) -> list[CommandLine]:
    """Split a run script into commands and retain their following operators."""
    lines = run.replace("\\\n", " ").splitlines()
    return [group for line in lines for group in _line_command_groups(line)]


def _next_shell_state(char: str, quote: str, *, escaped: bool) -> tuple[str, bool]:
    """Track shell quoting and escaping while preserving the original text."""
    if escaped:
        return quote, False
    if char == "\\" and quote != "'":
        return quote, True
    if char in "'\"":
        if not quote:
            return char, False
        if quote == char:
            return "", False
    return quote, False


def _protect_quoted_operators(line: str) -> str:
    """Mark quoted or escaped operators so shlex keeps them as arguments."""
    protected = []
    quote = ""
    escaped = False
    word_started = False
    replacements = str.maketrans(";&|#", "\ue000\ue001\ue002\ue003")
    for char in line:
        protect = quote or escaped or (char == "#" and word_started)
        marked = char.translate(replacements) if protect else char
        if not quote and not escaped:
            word_started = not (char.isspace() or char in ";&|")
        quote, escaped = _next_shell_state(char, quote, escaped=escaped)
        protected.append(marked)
    return "".join(protected)


def _line_command_groups(line: str) -> list[CommandLine]:
    """Split one shell line while preserving operators and quoted arguments."""
    lexer = shlex.shlex(
        _protect_quoted_operators(line), posix=True, punctuation_chars=";&|"
    )
    lexer.whitespace_split = True
    groups: list[CommandLine] = []
    current: list[str] = []
    for token in lexer:
        if token and not token.strip(";&|"):
            if current:
                groups.append((tuple(current), token))
                current = []
            continue
        if token in {"then", "do"} and not current:
            continue
        current.append(
            token.translate(str.maketrans("\ue000\ue001\ue002\ue003", ";&|#"))
        )
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
    subject: str,
    violations: list[str],
) -> list[CommandEntry]:
    """Return run commands with their step and within-step positions."""
    found: list[CommandEntry] = []
    for step_index, step in enumerate(steps):
        run = step.get("run")
        if not isinstance(run, str):
            continue
        try:
            groups = _command_groups(run)
        except ValueError as error:
            violations.append(
                f"{subject} step {step_index} has invalid shell syntax: {error}"
            )
            continue
        for command_index, (command, separator) in enumerate(groups):
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
    if not _is_command(command, "uv", "run", "--locked", "--no-sync", "python", "-c"):
        return False
    # Accept only known checks whose exit status is nonzero on a mismatch.
    try:
        tree = ast.dump(ast.parse(" ".join(command[6:])))
    except SyntaxError:
        return False
    return any(tree == ast.dump(ast.parse(check)) for check in VERSION_CHECK_PROGRAMS)


def _pytest_command(command: CommandGroup) -> bool:
    """Report whether a command runs the full suite without syncing the environment."""
    return command == ("uv", "run", "--locked", "--no-sync", "pytest", "-v")


def _unsafe_environment_command(command: CommandGroup) -> bool:
    """Report a command that can replace the Falcon override with the lockfile."""
    if _is_command(command, "make"):
        return True
    if _is_command(command, "uv", "pip", "install"):
        return any("falcon==" in argument.casefold() for argument in command[3:])
    if _is_command(command, "uv", "sync"):
        return True
    return _is_command(command, "uv", "run") and (
        "--no-sync" not in command or "make" in command[2:]
    )


def _is_enabled(value: object) -> bool:
    """Treat anything other than an absent or false value as enabled."""
    if value is None or value is False:
        return False
    return not (isinstance(value, str) and value.strip().casefold() == "false")


def _falcon_override(
    job: dict[str, object], steps: list[dict[str, object]], commands: list[CommandEntry]
) -> bool:
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
        "falcon==" in " ".join(command).casefold() for _, _, command, _ in commands
    )


def _job_condition_violations(job: dict[str, object]) -> list[str]:
    """Refuse lane-level conditions that can skip coverage of the CI triggers."""
    violations = []
    if "if" in job:
        violations.append(f"{MINIMUM_JOB} must not have a job-level if condition")
    if _is_enabled(job.get("continue-on-error")):
        violations.append(f"{MINIMUM_JOB} must not continue on error")
    return violations


def _environment_violations(job: dict[str, object], lower_bound: str) -> list[str]:
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
    checkout_index = positions["checkout"]
    if checkout_index is not None:
        inputs = as_mapping(steps[checkout_index].get("with"))
        if inputs is None or inputs.get("persist-credentials") is not False:
            violations.append(
                f"{MINIMUM_JOB} checkout must set persist-credentials: false"
            )
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
        ("verify", "verify Falcon with uv run --locked --no-sync"),
        ("pytest", "run pytest with uv run --locked --no-sync"),
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


def _skippable_command_violations(
    commands: list[CommandEntry], required_commands: dict[str, CommandEntry | None]
) -> list[str]:
    """Require each essential command to be the only command in its step."""
    return [
        f"{MINIMUM_JOB} {name} command must be the only command in its step"
        for name, entry in required_commands.items()
        if entry is not None
        and sum(step == entry[0] for step, _, _, _ in commands) != 1
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


def _minimum_job_violations(job: dict[str, object], lower_bound: str) -> list[str]:
    """Inspect the environment and ordered commands of the lower-bound job."""
    violations = _job_condition_violations(job)
    violations.extend(_environment_violations(job, lower_bound))
    steps = _steps(job, MINIMUM_JOB, violations)
    violations.extend(_step_condition_violations(steps))
    setup_positions, setup_violations = _setup_positions(steps)
    violations.extend(setup_violations)
    commands = _commands(steps, MINIMUM_JOB, violations)
    required_commands, command_violations = _required_commands(commands)
    violations.extend(command_violations)
    violations.extend(_step_order_violations(setup_positions, required_commands))
    violations.extend(_masked_command_violations(required_commands))
    violations.extend(_skippable_command_violations(commands, required_commands))
    violations.extend(_resync_violations(commands, required_commands))
    return violations


def _newest_job_violations(job: dict[str, object]) -> list[str]:
    """Keep the locked-version lane's build and dependency resolution intact."""
    violations = []
    steps = _steps(job, NEWEST_JOB, violations)
    commands = _commands(steps, NEWEST_JOB, violations)
    if not any(_is_command(command, "make", "build") for _, _, command, _ in commands):
        violations.append(f"{NEWEST_JOB} must continue to run make build")
    if _falcon_override(job, steps, commands):
        violations.append(f"{NEWEST_JOB} must not override the locked Falcon release")
    return violations


def falcon_minimum_lane_violations(
    source: WorkflowSource,
    *,
    project_metadata_path: pathlib.Path = ROOT / "pyproject.toml",
) -> list[str]:
    """Return every violation in the minimum-version and newest lanes.

    Parameters
    ----------
    source : WorkflowSource
        The workflow directory to inspect.
    project_metadata_path : pathlib.Path, optional
        Metadata supplying Falcon's lower bound; defaults to repository pyproject.toml.

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
        try:
            lower_bound = _project_falcon_lower_bound(project_metadata_path)
        except (
            WorkflowShapeError,
            tomllib.TOMLDecodeError,
            KeyError,
            TypeError,
            ValueError,
        ) as error:
            violations.append(
                f"{project_metadata_path} has invalid project metadata: {error}"
            )
        else:
            violations.extend(_minimum_job_violations(minimum_job, lower_bound))
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
    violations = falcon_minimum_lane_violations(source)
    expected = EXPECTED_REGRESSION_VIOLATIONS[case]
    assert any(expected in item for item in violations), f"{case}: {violations}"


def test_a_minimal_valid_minimum_lane_has_no_violations(
    tmp_path: pathlib.Path,
) -> None:
    """A small but effective lower-bound lane satisfies the inspection rule."""
    source = _temporary_workflow_source(tmp_path, valid_workflow())
    assert not falcon_minimum_lane_violations(source), (
        "a valid temporary lower-bound lane must satisfy the contract"
    )


@pytest.mark.parametrize(
    ("specifier", "expected"),
    [("falcon>=4,<5", "4.0.0"), ("falcon>=4,>=4.0.1,<5", "4.0.1")],
    ids=["short-release", "strongest-lower-bound"],
)
def test_falcon_lower_bound_normalisation(specifier: str, expected: str) -> None:
    """The strongest declared lower bound is padded to three release parts."""
    assert _normalise_falcon_lower_bound(Requirement(specifier)) == expected, (
        f"incorrect Falcon lower bound for {specifier}"
    )


def test_falcon_lower_bound_requires_a_lower_specifier() -> None:
    """An upper bound alone cannot define the minimum CI version."""
    with pytest.raises(ValueError, match="Falcon dependency has no lower bound"):
        _normalise_falcon_lower_bound(Requirement("falcon<5"))


@pytest.mark.parametrize(
    "program",
    [
        (
            "import importlib.metadata, os, sys; "
            "actual = importlib.metadata.version('falcon'); "
            "expected = os.environ['FALCON_MINIMUM_VERSION']; "
            "sys.exit(0 if actual != expected else 1)"
        ),
        (
            "import importlib.metadata, os, sys; "
            "actual = importlib.metadata.version('falcon'); "
            "expected = os.environ['FALCON_MINIMUM_VERSION']; "
            "actual == expected; sys.exit(0)"
        ),
    ],
    ids=["inverted-result", "unconditional-success"],
)
def test_version_check_must_fail_on_a_mismatch(program: str) -> None:
    """A comparison alone does not prove the process fails on a mismatch."""
    assert not _version_check_command((
        "uv",
        "run",
        "--locked",
        "--no-sync",
        "python",
        "-c",
        program,
    )), "incorrect version check was accepted"


def test_malformed_shell_is_a_named_violation(tmp_path: pathlib.Path) -> None:
    """An unparseable step reports its job and position rather than raising."""
    workflow = valid_workflow()
    jobs = typ.cast("dict[str, object]", workflow["jobs"])
    job = typ.cast("dict[str, object]", jobs[MINIMUM_JOB])
    steps = typ.cast("list[dict[str, object]]", job["steps"])
    steps.append({"run": "echo 'unterminated"})
    violations = falcon_minimum_lane_violations(
        _temporary_workflow_source(tmp_path, workflow)
    )
    assert any(
        "falcon-minimum step 7" in item and "shell" in item for item in violations
    ), f"missing shell diagnostic: {violations}"


def test_checkout_must_not_persist_credentials(tmp_path: pathlib.Path) -> None:
    """The minimum lane must not retain checkout credentials for the suite."""
    workflow = valid_workflow()
    jobs = typ.cast("dict[str, object]", workflow["jobs"])
    job = typ.cast("dict[str, object]", jobs[MINIMUM_JOB])
    steps = typ.cast("list[dict[str, object]]", job["steps"])
    del steps[0]["with"]
    source = _temporary_workflow_source(tmp_path, workflow)
    assert any(
        "persist-credentials" in item for item in falcon_minimum_lane_violations(source)
    ), "checkout must disable credential persistence"


def test_temporary_metadata_controls_the_lower_bound(tmp_path: pathlib.Path) -> None:
    """Synthetic workflows are checked against their own project metadata."""
    source = _temporary_workflow_source(tmp_path, valid_workflow())
    metadata = tmp_path / "pyproject.toml"
    metadata.write_text(
        '[project]\ndependencies = ["falcon>=4.1,<5"]\n', encoding="utf-8"
    )
    violations = falcon_minimum_lane_violations(source, project_metadata_path=metadata)
    assert any("4.1.0" in item for item in violations), (
        f"wrong lower bound: {violations}"
    )


@pytest.mark.parametrize(
    "content", [b"\xff", b"[project\n"], ids=["invalid-utf8", "invalid-toml"]
)
def test_invalid_metadata_is_reported(tmp_path: pathlib.Path, content: bytes) -> None:
    """Invalid metadata becomes an actionable violation rather than a traceback."""
    source = _temporary_workflow_source(tmp_path, valid_workflow())
    metadata = tmp_path / "pyproject.toml"
    metadata.write_bytes(content)
    violations = falcon_minimum_lane_violations(source, project_metadata_path=metadata)
    assert any("pyproject.toml" in item for item in violations), (
        f"missing metadata error: {violations}"
    )


@given(
    st.lists(
        st.tuples(
            st.sampled_from([">", ">="]),
            st.lists(st.integers(0, 9), min_size=1, max_size=3),
        ),
        min_size=1,
        max_size=5,
    )
)
def test_generated_lower_bounds_choose_the_strongest(
    bounds: list[tuple[str, list[int]]],
) -> None:
    """Multiple generated lower bounds select the strongest release."""
    specifier = "falcon" + ",".join(
        operator + ".".join(map(str, release)) for operator, release in bounds
    )
    assert _normalise_falcon_lower_bound(Requirement(specifier)) == ".".join(
        map(str, max(tuple([*release, 0, 0][:3]) for _, release in bounds))
    ), f"incorrect generated lower bound: {bounds}"


def test_command_step_finds_the_unique_checked_command() -> None:
    """Fixture rewrites share the same checked step and command."""
    step, command = _command_step(valid_workflow(), "pytest -v")
    assert step["run"] == command == "uv run --locked --no-sync pytest -v", "wrong step"
    with pytest.raises(ValueError, match="expected one"):
        _command_step(valid_workflow(), "missing marker")


@pytest.mark.parametrize("value", [None, False, "false", " FALSE "])
def test_disabled_continue_on_error_values(value: object) -> None:
    """Absent and literal false values leave failures visible."""
    assert not _is_enabled(value), f"unexpected enabled value: {value!r}"


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        (
            'echo "a;b" || true',
            [(("echo", "a;b"), "||"), (("true",), None)],
        ),
        (
            "then uv sync && echo done",
            [(("uv", "sync"), "&&"), (("echo", "done"), None)],
        ),
        (
            "echo ready | tee log &",
            [(("echo", "ready"), "|"), (("tee", "log"), "&")],
        ),
        ("echo a#b '#' ''# \\# # comment", [(("echo", "a#b", "#", "#", "#"), None)]),
    ],
    ids=["quoted-separator", "control-keyword", "pipe-and-background", "hash-comments"],
)
def test_shell_line_commands_preserve_operators(
    line: str, expected: list[CommandLine]
) -> None:
    """Quoted punctuation stays in arguments and shell operators stay attached."""
    assert _line_command_groups(line) == expected, f"incorrect shell grouping: {line}"


def test_shell_commands_join_continuations_and_keep_lines_separate() -> None:
    """A continued command and the following line remain distinct commands."""
    expected = [
        (("uv", "run", "--no-sync", "pytest", "-v"), None),
        (("printf", "a;b"), None),
    ]
    assert (
        _command_groups("uv run \\\n --no-sync pytest -v\nprintf 'a;b'") == expected
    ), "continuations must join without joining separate shell lines"


@given(
    st.lists(
        st.text(alphabet="abcXYZ09 ;|&#'\"", min_size=1, max_size=12),
        min_size=1,
        max_size=4,
    ),
    st.sampled_from([";", "&&", "||", "|", "&", "\n"]),
    st.sampled_from([" ", " \\\n"]),
    st.lists(
        st.text(alphabet="abcXYZ09 ;|&#'\"", min_size=1, max_size=12),
        min_size=1,
        max_size=4,
    ),
)
def test_shell_grouping_preserves_generated_quoted_arguments(
    left: list[str], operator: str, joiner: str, right: list[str]
) -> None:
    """The parser preserves generated argv and supported shell operators."""
    script = joiner.join(map(shlex.quote, ["echo", *left])) + f" {operator} "
    script += joiner.join(map(shlex.quote, ["printf", *right]))
    expected = [
        (("echo", *left), None if operator == "\n" else operator),
        (("printf", *right), None),
    ]
    assert _command_groups(script) == expected, f"incorrect shell grouping: {script}"


def test_quoted_and_escaped_operators_are_protected() -> None:
    """Only operators outside quotes and escapes become separators."""
    expected = [(("echo", "&", ";", "|"), "&&"), (("printf", "done"), None)]
    assert _command_groups("echo '&' \\; '|' && printf done") == expected, (
        "quoted and escaped punctuation must remain arguments"
    )


@pytest.mark.parametrize(
    "case",
    SHELL_STATE_CASES,
    ids=["open", "single-quoted-backslash", "escape", "escaped-quote", "close"],
)
def test_shell_quote_state_tracks_escapes_and_quotes(
    case: tuple[str, str, bool, tuple[str, bool]],
) -> None:
    """Quoted text and escaped quotes retain their shell state."""
    char, quote, escaped, expected = case
    assert _next_shell_state(char, quote, escaped=escaped) == expected, (
        f"incorrect shell state for {case!r}"
    )
