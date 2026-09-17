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
RATCHET_ACTION = "leynos/shared-actions/.github/actions/generate-coverage@abc123"
UPLOAD_ACTION = "leynos/shared-actions/.github/actions/upload-codescene-coverage@abc123"
CHECKOUT_ACTION = "actions/checkout@v7"
MAIN_ONLY_TRIGGER: dict[typ.Any, typ.Any] = {"push": {"branches": ["main"]}}
PULL_REQUEST_TRIGGER: dict[typ.Any, typ.Any] = {"pull_request": None}


def _as_mapping(value: object, message: str) -> dict[typ.Any, typ.Any]:
    """Assert that ``value`` is a mapping and narrow its static type."""
    assert isinstance(value, dict), message
    return typ.cast("dict[typ.Any, typ.Any]", value)


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


def _job_steps(job: object) -> cabc.Iterator[dict[typ.Any, typ.Any]]:
    """Yield inline step mappings from one job."""
    if not isinstance(job, dict):
        return

    job_mapping = typ.cast("dict[typ.Any, typ.Any]", job)
    steps = job_mapping.get("steps", [])
    if not isinstance(steps, list):
        return

    for step in steps:
        if isinstance(step, dict):
            yield typ.cast("dict[typ.Any, typ.Any]", step)


def _steps(
    workflow: dict[typ.Any, typ.Any],
) -> cabc.Iterator[dict[typ.Any, typ.Any]]:
    """Yield inline steps from jobs that expose them."""
    jobs = _as_mapping(workflow.get("jobs"), "workflow must declare a jobs mapping")
    for job in jobs.values():
        yield from _job_steps(job)


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


def _uploads_codescene_coverage(step: dict[typ.Any, typ.Any]) -> bool:
    """Return whether a step explicitly uploads coverage to CodeScene."""
    if not _uses(step, "upload-codescene-coverage"):
        return False
    inputs = step.get("with")
    return isinstance(inputs, dict) and inputs.get("mode") == "upload"


def _is_main_publisher(workflow: dict[typ.Any, typ.Any]) -> bool:
    """Return whether a workflow is a main-only ratcheted coverage publisher."""
    if not _is_main_only_trigger(workflow):
        return False
    steps = list(_steps(workflow))
    return any(_ratcheting_coverage(step) for step in steps) and any(
        _uploads_codescene_coverage(step) for step in steps
    )


def _has_ratcheted_coverage(steps: list[dict[typ.Any, typ.Any]]) -> bool:
    """Return whether coverage steps are absent or at least one is ratcheted."""
    coverage_steps = [step for step in steps if _uses(step, "generate-coverage")]
    return not coverage_steps or any(
        _ratcheting_coverage(step) for step in coverage_steps
    )


def _requests_full_history(step: dict[typ.Any, typ.Any]) -> bool:
    """Return whether a step asks checkout for the full Git history."""
    inputs = step.get("with")
    return isinstance(inputs, dict) and inputs.get("fetch-depth") in {0, "0"}


def _checkout_steps_avoid_full_history(
    steps: list[dict[typ.Any, typ.Any]],
) -> bool:
    """Return whether every checkout step avoids fetching full history."""
    return all(
        not _requests_full_history(step)
        for step in steps
        if _uses(step, "actions/checkout")
    )


def _step(
    uses: str, inputs: dict[typ.Any, typ.Any] | None = None
) -> dict[typ.Any, typ.Any]:
    """Build an inline workflow step referencing ``uses``."""
    return {"uses": uses, "with": {} if inputs is None else inputs}


def _workflow(
    trigger: dict[typ.Any, typ.Any], steps: list[object]
) -> dict[typ.Any, typ.Any]:
    """Build a workflow whose single job declares ``trigger`` and ``steps``."""
    return {"on": trigger, "jobs": {"job": {"steps": steps}}}


def test_job_steps_yield_only_mapping_entries() -> None:
    """Yield direct ``steps`` mapping members and skip every other value."""
    job = {"steps": [{"uses": CHECKOUT_ACTION}, "not-a-step", ["nested"]]}

    assert list(_job_steps(job)) == [{"uses": CHECKOUT_ACTION}]


def test_job_steps_ignore_non_mapping_jobs() -> None:
    """Yield nothing when the job itself is not a mapping."""
    assert list(_job_steps("not-a-job")) == []
    assert list(_job_steps(None)) == []
    assert list(_job_steps([{"uses": CHECKOUT_ACTION}])) == []


def test_job_steps_ignore_non_list_steps() -> None:
    """Yield nothing when ``steps`` is not a list."""
    assert list(_job_steps({"steps": "not-a-list"})) == []
    assert list(_job_steps({"steps": {"uses": CHECKOUT_ACTION}})) == []
    assert list(_job_steps({"no-steps": []})) == []


def test_job_steps_do_not_traverse_nested_step_lists() -> None:
    """Yield only the direct step entry, never the steps nested inside it."""
    nested = {"steps": [{"uses": CHECKOUT_ACTION}]}

    assert list(_job_steps({"steps": [nested]})) == [nested]


def test_steps_collect_direct_job_level_steps_only() -> None:
    """Collect steps from mapping jobs and ignore every other job value."""
    workflow = _workflow(MAIN_ONLY_TRIGGER, [{"uses": CHECKOUT_ACTION}])
    workflow["jobs"]["second"] = {"steps": [{"uses": RATCHET_ACTION}]}
    workflow["jobs"]["metadata"] = "not-a-job"

    assert list(_steps(workflow)) == [
        {"uses": CHECKOUT_ACTION},
        {"uses": RATCHET_ACTION},
    ]


def test_uploads_codescene_coverage_requires_the_upload_action() -> None:
    """Accept only an explicit CodeScene upload action."""
    assert _uploads_codescene_coverage(_step(UPLOAD_ACTION, {"mode": "upload"}))


def test_uploads_codescene_coverage_requires_upload_mode() -> None:
    """Reject other actions, other modes, and a non-mapping ``with`` value."""
    assert not _uploads_codescene_coverage(_step(UPLOAD_ACTION))
    assert not _uploads_codescene_coverage(_step(UPLOAD_ACTION, {"mode": "check"}))
    assert not _uploads_codescene_coverage(_step(CHECKOUT_ACTION, {"mode": "upload"}))
    assert not _uploads_codescene_coverage({"uses": UPLOAD_ACTION, "with": "upload"})


def test_is_main_publisher_accepts_a_main_only_ratcheted_uploader() -> None:
    """Accept a main-only workflow that ratchets and explicitly uploads."""
    workflow = _workflow(
        MAIN_ONLY_TRIGGER,
        [
            _step(RATCHET_ACTION, {"with-ratchet": "true"}),
            _step(UPLOAD_ACTION, {"mode": "upload"}),
        ],
    )

    assert _is_main_publisher(workflow)


def test_is_main_publisher_rejects_a_non_main_only_workflow() -> None:
    """Reject an otherwise valid publisher that also runs on pull requests."""
    workflow = _workflow(
        PULL_REQUEST_TRIGGER,
        [
            _step(RATCHET_ACTION, {"with-ratchet": "true"}),
            _step(UPLOAD_ACTION, {"mode": "upload"}),
        ],
    )

    assert not _is_main_publisher(workflow)


def test_is_main_publisher_rejects_a_missing_ratchet_step() -> None:
    """Reject a main-only uploader that never ratchets coverage."""
    workflow = _workflow(
        MAIN_ONLY_TRIGGER,
        [_step(RATCHET_ACTION), _step(UPLOAD_ACTION, {"mode": "upload"})],
    )

    assert not _is_main_publisher(workflow)


def test_is_main_publisher_rejects_a_missing_upload_step() -> None:
    """Reject a main-only ratcheting workflow that never uploads."""
    workflow = _workflow(
        MAIN_ONLY_TRIGGER, [_step(RATCHET_ACTION, {"with-ratchet": "true"})]
    )

    assert not _is_main_publisher(workflow)


def test_is_main_publisher_ignores_steps_offered_to_a_reusable_workflow() -> None:
    """Ignore publish steps passed to a reusable workflow instead of run inline."""
    workflow = _workflow(MAIN_ONLY_TRIGGER, [])
    workflow["jobs"]["publisher"] = {
        "uses": "leynos/shared-actions/.github/workflows/coverage.yml@abc123",
        "with": {
            "steps": [
                _step(RATCHET_ACTION, {"with-ratchet": "true"}),
                _step(UPLOAD_ACTION, {"mode": "upload"}),
            ]
        },
    }

    assert not _is_main_publisher(workflow)


def test_has_ratcheted_coverage_accepts_absent_coverage_steps() -> None:
    """Accept steps that contain no coverage step at all."""
    assert _has_ratcheted_coverage([])
    assert _has_ratcheted_coverage([_step(CHECKOUT_ACTION)])


def test_has_ratcheted_coverage_accepts_a_ratcheted_step() -> None:
    """Accept a coverage step that enables the local ratchet."""
    steps = [_step(CHECKOUT_ACTION), _step(RATCHET_ACTION, {"with-ratchet": True})]

    assert _has_ratcheted_coverage(steps)


def test_has_ratcheted_coverage_accepts_one_ratcheted_step_among_many() -> None:
    """Accept as soon as any coverage step enables the local ratchet."""
    steps = [_step(RATCHET_ACTION), _step(RATCHET_ACTION, {"with-ratchet": "true"})]

    assert _has_ratcheted_coverage(steps)


def test_has_ratcheted_coverage_rejects_an_unratcheted_step() -> None:
    """Reject coverage steps that never enable the local ratchet."""
    assert not _has_ratcheted_coverage([_step(RATCHET_ACTION)])
    assert not _has_ratcheted_coverage(
        [_step(RATCHET_ACTION, {"with-ratchet": "false"})]
    )
    assert not _has_ratcheted_coverage(
        [_step(RATCHET_ACTION), _step(RATCHET_ACTION, {"with-ratchet": "false"})]
    )


def test_checkout_steps_avoid_full_history_accepts_non_checkout_steps() -> None:
    """Accept a step list that contains no checkout step."""
    assert _checkout_steps_avoid_full_history([])
    assert _checkout_steps_avoid_full_history([_step(RATCHET_ACTION)])


def test_checkout_steps_avoid_full_history_accepts_shallow_checkouts() -> None:
    """Accept checkout steps that never request zero fetch depth."""
    assert _checkout_steps_avoid_full_history([_step(CHECKOUT_ACTION)])
    assert _checkout_steps_avoid_full_history(
        [_step(CHECKOUT_ACTION, {"fetch-depth": 1})]
    )
    assert _checkout_steps_avoid_full_history(
        [_step(CHECKOUT_ACTION, {"fetch-depth": "1"})]
    )


def test_checkout_steps_avoid_full_history_treats_non_mapping_with_as_safe() -> None:
    """Accept a checkout step whose ``with`` value is not a mapping."""
    assert _checkout_steps_avoid_full_history([{"uses": CHECKOUT_ACTION, "with": "0"}])


def test_checkout_steps_avoid_full_history_rejects_numeric_zero_depth() -> None:
    """Reject a checkout step that requests full history with an int zero."""
    steps = [_step(CHECKOUT_ACTION, {"fetch-depth": 0})]

    assert not _checkout_steps_avoid_full_history(steps)


def test_checkout_steps_avoid_full_history_rejects_quoted_zero_depth() -> None:
    """Reject a checkout step that requests full history with a quoted zero."""
    steps = [_step(CHECKOUT_ACTION, {"fetch-depth": "0"})]

    assert not _checkout_steps_avoid_full_history(steps)


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
        assert _has_ratcheted_coverage(steps), (
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
        assert _checkout_steps_avoid_full_history(steps), (
            f"{path} must not request full history for CodeScene"
        )


def test_main_publisher_is_main_only_ratcheted_and_explicitly_uploads() -> None:
    """Require one main-only publisher to ratchet and select upload mode."""
    publishers: list[Path] = [
        path for path, workflow in _workflows() if _is_main_publisher(workflow)
    ]

    assert publishers == [WORKFLOWS_DIR / "coverage-main.yml"], (
        "exactly coverage-main.yml must publish ratcheted coverage on main; "
        f"found {publishers!r}"
    )
