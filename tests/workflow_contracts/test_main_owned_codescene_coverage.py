"""Contract tests for the main-owned CodeScene coverage topology.

Pull requests use the local ratchet produced by the shared coverage action.
Only the main-branch publisher may contact CodeScene, which keeps pull-request
jobs independent of external changed-line coverage state and its secret.

A pull-request lane that declares no coverage step of its own is exempt from
the ratchet requirement, because a job that delegates wholesale to a reusable
workflow exposes none of its steps to this scan. ``dependabot-automerge.yml``
is the one such lane, and a companion test pins the exempt set so the
requirement cannot quietly stop applying to a lane that does own coverage.
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
# Identity checks match the action *path* only: the trailing ``@ref`` is a pin
# that must not change whether a step is recognised as the expected action.
RATCHET_ACTION_PATH = "leynos/shared-actions/.github/actions/generate-coverage"
UPLOAD_ACTION_PATH = "leynos/shared-actions/.github/actions/upload-codescene-coverage"
CHECKOUT_ACTION_PATH = "actions/checkout"
CODE_SCENE_FRAGMENT = "codescene"
RATCHET_ACTION = f"{RATCHET_ACTION_PATH}@abc123"
UPLOAD_ACTION = f"{UPLOAD_ACTION_PATH}@abc123"
CHECKOUT_ACTION = f"{CHECKOUT_ACTION_PATH}@v7"
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


def _uses(step: dict[typ.Any, typ.Any], action_path: str) -> bool:
    """Return whether a step invokes exactly ``action_path``.

    Only the path before any ``@`` is compared, so a repinned action stays
    recognised. A substring test would also match a look-alike action that
    merely embeds the expected path.
    """
    uses = step.get("uses")
    return isinstance(uses, str) and uses.partition("@")[0] == action_path


def _uses_fragment(step: dict[typ.Any, typ.Any], fragment: str) -> bool:
    """Return whether a step's action reference contains ``fragment``.

    Reserved for the deliberately loose CodeScene sweep, which is meant to
    catch any owner, action, or ref that mentions the vendor.
    """
    uses = step.get("uses")
    return isinstance(uses, str) and fragment in uses


def _ratcheting_coverage(step: dict[typ.Any, typ.Any]) -> bool:
    """Return whether a coverage step enables the local ratchet."""
    inputs = step.get("with")
    if not _uses(step, RATCHET_ACTION_PATH) or not isinstance(inputs, dict):
        return False
    return inputs.get("with-ratchet") in {True, "true"}


def _uploads_codescene_coverage(step: dict[typ.Any, typ.Any]) -> bool:
    """Return whether a step explicitly uploads coverage to CodeScene."""
    if not _uses(step, UPLOAD_ACTION_PATH):
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
    """Return whether at least one coverage step enables the local ratchet."""
    return any(_ratcheting_coverage(step) for step in steps)


def _owns_coverage_here(steps: list[dict[typ.Any, typ.Any]]) -> bool:
    """Return whether this workflow declares its own coverage step."""
    return any(_uses_fragment(step, "generate-coverage") for step in steps)


def _checkout_step_history_depths(
    steps: list[dict[typ.Any, typ.Any]],
) -> list[object]:
    """Return the ``fetch-depth`` each checkout step sets, or ``None``.

    A depth of zero means full history. That is legitimate here: the coverage
    action needs the pull request's merge base to measure changed lines. The
    depths are therefore reported rather than judged, so the contract below
    can state which of them the estate actually relies on.
    """
    return [
        step["with"].get("fetch-depth") if isinstance(step.get("with"), dict) else None
        for step in steps
        if _uses(step, CHECKOUT_ACTION_PATH)
    ]


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


def test_uses_matches_the_action_path_exactly() -> None:
    """Match the path before ``@`` and ignore the pinning ref."""
    assert _uses({"uses": RATCHET_ACTION_PATH}, RATCHET_ACTION_PATH)
    assert _uses({"uses": f"{RATCHET_ACTION_PATH}@0000000"}, RATCHET_ACTION_PATH)


def test_uses_rejects_a_look_alike_action() -> None:
    """Reject an action whose path merely embeds the expected one."""
    assert not _uses(_step(f"evil/{RATCHET_ACTION_PATH}"), RATCHET_ACTION_PATH)
    assert not _uses(_step(f"{RATCHET_ACTION_PATH}-extra"), RATCHET_ACTION_PATH)
    assert not _uses(_step(UPLOAD_ACTION), RATCHET_ACTION_PATH)
    assert not _uses({"uses": 7}, RATCHET_ACTION_PATH)
    assert not _uses({"with": {}}, RATCHET_ACTION_PATH)


def test_uses_fragment_matches_any_reference_mentioning_it() -> None:
    """Keep the loose sweep for the intentionally broad CodeScene check."""
    assert _uses_fragment(_step("evil/codescene-action@v1"), CODE_SCENE_FRAGMENT)
    assert _uses_fragment(_step("codescene-upload"), CODE_SCENE_FRAGMENT)
    assert not _uses_fragment(_step(CHECKOUT_ACTION), CODE_SCENE_FRAGMENT)
    assert not _uses_fragment({"uses": 7}, CODE_SCENE_FRAGMENT)


def test_owns_coverage_here_detects_a_declared_coverage_step() -> None:
    """Recognise coverage by fragment, so any pinning ref still counts."""
    assert _owns_coverage_here([_step(CHECKOUT_ACTION), _step(RATCHET_ACTION)])
    assert not _owns_coverage_here([_step(CHECKOUT_ACTION)])
    assert not _owns_coverage_here([])


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


def test_has_ratcheted_coverage_rejects_absent_coverage_steps() -> None:
    """Reject step lists that never generate coverage at all."""
    assert not _has_ratcheted_coverage([])
    assert not _has_ratcheted_coverage([_step(CHECKOUT_ACTION)])


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


def test_checkout_step_history_depths_reports_only_checkout_steps() -> None:
    """Report the depth of each checkout step and ignore every other step."""
    steps = [_step(RATCHET_ACTION), _step(CHECKOUT_ACTION, {"fetch-depth": 0})]

    assert _checkout_step_history_depths(steps) == [0]
    assert _checkout_step_history_depths([]) == []
    assert _checkout_step_history_depths([_step(RATCHET_ACTION)]) == []


def test_checkout_step_history_depths_defaults_to_none() -> None:
    """Report ``None`` when a checkout step sets no depth or no mapping."""
    steps = [
        _step(CHECKOUT_ACTION, {"fetch-depth": "0"}),
        _step(CHECKOUT_ACTION),
        {"uses": CHECKOUT_ACTION, "with": "0"},
    ]

    assert _checkout_step_history_depths(steps) == ["0", None, None]


def test_pull_request_workflows_use_the_local_ratchet_only() -> None:
    """Keep pull-request coverage local and exclude CodeScene dependencies."""
    pull_request_workflows = [
        (path, workflow)
        for path, workflow in _workflows()
        # Exempt a workflow that declares no coverage step here. Such a
        # workflow delegates wholesale to a reusable workflow, as
        # dependabot-automerge.yml does, so this file's scan cannot see what
        # it ultimately runs and must not invent a ratchet requirement for it.
        # Coverage-owning pull-request workflows remain covered, and the
        # non-empty assertion below fails if this ever exempts them all.
        if _has_pull_request_trigger(workflow)
        and _owns_coverage_here(list(_steps(workflow)))
    ]
    assert pull_request_workflows, "at least one pull-request workflow is required"

    for path, workflow in pull_request_workflows:
        steps = list(_steps(workflow))
        assert _has_ratcheted_coverage(steps), (
            f"{path} pull-request coverage must enable with-ratchet"
        )
        assert not any(_uses_fragment(step, CODE_SCENE_FRAGMENT) for step in steps), (
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
        # Checkout depth is deliberately not asserted here. ci.yml requests
        # full history so the coverage action can reach the pull request's
        # merge base, and that depth serves the local ratchet, not CodeScene.
        # The boundary defended above is the absent CodeScene call, command,
        # project URL, and credential.
        assert 0 in _checkout_step_history_depths(steps), (
            f"{path} must request full history: the ratchet needs the merge base"
        )


def test_only_the_named_lane_generates_pull_request_coverage() -> None:
    """Pin both sides of the delegation exemption in the test above.

    That exemption is only safe while it exempts the delegating automerge lane
    and no lane that actually generates coverage. Naming the expected set
    turns drift in either direction into a failure here, rather than into an
    assertion that quietly drops out of the loop.
    """
    owners = sorted(
        path.name
        for path, workflow in _workflows()
        if _has_pull_request_trigger(workflow)
        and _owns_coverage_here(list(_steps(workflow)))
    )

    assert owners == ["ci.yml"], (
        "ci.yml must be the only pull-request workflow that generates "
        f"coverage here; found {owners!r}"
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


def test_the_publisher_ratchets_its_own_report() -> None:
    """Require the publisher's coverage step to enable the ratchet itself.

    The publisher test above only asks whether *some* step ratchets, so it
    would still pass if the publisher kept its upload while dropping the
    ratchet from its generation step. The baseline the pull-request lane is
    measured against is written by that step, and a baseline that is not
    ratcheted is not the measurement the ratchet compares against.
    """
    publisher = _load(WORKFLOWS_DIR / "coverage-main.yml")
    ratcheting = [step for step in _steps(publisher) if _ratcheting_coverage(step)]

    assert ratcheting, (
        "coverage-main.yml must generate coverage with with-ratchet enabled; "
        "no ratcheting generate-coverage step was found"
    )
