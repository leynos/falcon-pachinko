"""Contract tests for the main-owned CodeScene coverage topology.

Pull requests use the local ratchet produced by the shared coverage action.
Only the main-branch publisher may contact CodeScene, which keeps pull-request
jobs independent of external changed-line coverage state and its secret.
"""

from __future__ import annotations

import re
import typing as typ
from pathlib import Path

import yaml

if typ.TYPE_CHECKING:
    import collections.abc as cabc

WORKFLOWS_DIR = Path(__file__).resolve().parents[2] / ".github" / "workflows"
CODE_SCENE_COMMAND_RE = re.compile(r"\bcs-coverage\s+(?:check|upload)\b")
PULL_REQUEST_EVENTS = ("pull_request", "pull_request_target")
PULL_REQUEST_EVENT_SET = set(PULL_REQUEST_EVENTS)


def _as_mapping(value: object, message: str) -> dict[typ.Any, typ.Any]:
    """Assert that ``value`` is a mapping and narrow its static type."""
    assert isinstance(value, dict), message
    return typ.cast("dict[typ.Any, typ.Any]", value)


def _iter_mappings(value: object) -> cabc.Iterator[dict[typ.Any, typ.Any]]:
    """Yield every mapping nested in a decoded workflow document."""
    if isinstance(value, dict):
        mapping = typ.cast("dict[typ.Any, typ.Any]", value)
        yield mapping
        for nested in mapping.values():
            yield from _iter_mappings(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from _iter_mappings(nested)


def _load(path: Path) -> dict[typ.Any, typ.Any]:
    """Parse a workflow file into a mapping."""
    return _as_mapping(
        yaml.safe_load(path.read_text(encoding="utf-8")),
        f"{path} must parse to a mapping",
    )


def _triggers(workflow: dict[typ.Any, typ.Any]) -> object:
    """Return the workflow trigger declaration, accounting for YAML 1.1."""
    return workflow.get("on", workflow.get(True))


def _has_pull_request_trigger(workflow: dict[typ.Any, typ.Any]) -> bool:
    """Return whether a workflow runs for a pull-request event."""
    triggers = _triggers(workflow)
    if isinstance(triggers, str):
        return triggers in PULL_REQUEST_EVENT_SET
    if isinstance(triggers, list):
        return any(event in PULL_REQUEST_EVENT_SET for event in triggers)
    if isinstance(triggers, dict):
        return any(event in triggers for event in PULL_REQUEST_EVENTS)
    return False


def _workflows() -> list[tuple[Path, dict[typ.Any, typ.Any]]]:
    """Load every YAML workflow in the repository."""
    paths = sorted((*WORKFLOWS_DIR.glob("*.yml"), *WORKFLOWS_DIR.glob("*.yaml")))
    return [(path, _load(path)) for path in paths]


def _steps(
    workflow: dict[typ.Any, typ.Any],
) -> cabc.Iterator[dict[typ.Any, typ.Any]]:
    """Yield inline steps from jobs that expose them."""
    jobs = _as_mapping(workflow.get("jobs"), "workflow must declare a jobs mapping")
    for job in jobs.values():
        if not isinstance(job, dict):
            continue
        steps = job.get("steps", [])
        if isinstance(steps, list):
            for step in steps:
                if isinstance(step, dict):
                    yield typ.cast("dict[typ.Any, typ.Any]", step)


def _contains_text(value: object, pattern: re.Pattern[str]) -> bool:
    """Return whether any scalar nested in ``value`` matches ``pattern``."""
    if isinstance(value, str):
        return pattern.search(value) is not None
    return any(_contains_text(nested, pattern) for nested in _iter_values(value))


def _iter_values(value: object) -> cabc.Iterator[object]:
    """Yield nested mapping values and list members."""
    if isinstance(value, dict):
        yield from value.keys()
        yield from value.values()
    elif isinstance(value, list):
        yield from value


def _is_main_only_trigger(workflow: dict[typ.Any, typ.Any]) -> bool:
    """Return whether ``workflow`` runs on pushes to main only."""
    triggers = _triggers(workflow)
    if not isinstance(triggers, dict):
        return False
    trigger_mapping = typ.cast("dict[typ.Any, typ.Any]", triggers)
    if any(trigger not in {"push", "workflow_dispatch"} for trigger in trigger_mapping):
        return False
    push = trigger_mapping.get("push")
    if not isinstance(push, dict):
        return False
    return push.get("branches") == ["main"]


def _uses(step: dict[typ.Any, typ.Any], action: str) -> bool:
    """Return whether an inline step references ``action``."""
    uses = step.get("uses")
    return isinstance(uses, str) and action in uses


def _ratcheting_coverage(step: dict[typ.Any, typ.Any]) -> bool:
    """Return whether a coverage step enables the local ratchet."""
    inputs = step.get("with")
    if not _uses(step, "generate-coverage") or not isinstance(inputs, dict):
        return False
    return inputs.get("with-ratchet") in {True, "true"}


def test_pull_request_workflows_use_the_local_ratchet_only() -> None:
    """Keep pull-request coverage local and exclude CodeScene dependencies."""
    pull_request_workflows = [
        (path, workflow)
        for path, workflow in _workflows()
        if _has_pull_request_trigger(workflow)
    ]
    assert pull_request_workflows, "at least one pull-request workflow is required"

    for path, workflow in pull_request_workflows:
        steps = list(_steps(workflow))
        coverage_steps = [step for step in steps if _uses(step, "generate-coverage")]
        if coverage_steps:
            assert any(_ratcheting_coverage(step) for step in coverage_steps), (
                f"{path} pull-request coverage must enable with-ratchet"
            )
        assert not any(_uses(step, "codescene") for step in steps), (
            f"{path} must not invoke a CodeScene action"
        )
        assert not any(
            isinstance(step.get("run"), str)
            and CODE_SCENE_COMMAND_RE.search(step["run"]) is not None
            for step in steps
        ), f"{path} must not run cs-coverage check or upload"
        assert not _contains_text(
            workflow, re.compile(r"project-url", re.IGNORECASE)
        ), f"{path} must not carry a CodeScene project URL"
        assert not _contains_text(workflow, re.compile(r"CS_ACCESS_TOKEN")), (
            f"{path} must not receive CS_ACCESS_TOKEN"
        )
        for step in steps:
            if not _uses(step, "actions/checkout"):
                continue
            inputs = step.get("with")
            assert not isinstance(inputs, dict) or inputs.get("fetch-depth") not in {
                0,
                "0",
            }, f"{path} must not request full history for CodeScene"


def test_main_publisher_is_main_only_ratcheted_and_explicitly_uploads() -> None:
    """Require one main-only publisher to ratchet and select upload mode."""
    publishers: list[Path] = []
    for path, workflow in _workflows():
        if not _is_main_only_trigger(workflow):
            continue
        steps = list(_steps(workflow))
        has_ratchet = any(_ratcheting_coverage(step) for step in steps)
        has_upload = any(
            _uses(step, "upload-codescene-coverage")
            and isinstance(step.get("with"), dict)
            and step["with"].get("mode") == "upload"
            for step in steps
        )
        if has_ratchet and has_upload:
            publishers.append(path)

    assert publishers == [WORKFLOWS_DIR / "coverage-main.yml"], (
        "exactly coverage-main.yml must publish ratcheted coverage on main; "
        f"found {publishers!r}"
    )
