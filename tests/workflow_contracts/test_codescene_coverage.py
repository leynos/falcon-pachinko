"""CV-005: main owns every CodeScene interaction.

A pull-request lane measures coverage for its own ratchet and does nothing else
with it. It carries no CodeScene action, no ``cs-coverage`` command and no
``CS_ACCESS_TOKEN``. One workflow, reachable only from a push to ``main`` or a
dispatch, generates the trunk report and uploads it.

The separation is not tidiness. Between 2026-09-16 and 2026-09-18 an unpinned
``cs-coverage`` could not parse its own cobertura output, and because the check
ran inside the merge gate every pull request in this repository was blocked on
a step with nothing to say about the change under review. A pull-request lane
that cannot contact CodeScene cannot be stopped by CodeScene.

Both lanes must measure the same thing, or the baseline the trunk writes is not
the baseline the ratchet should compare against. The inputs are held equal here
rather than trusted to have been copied correctly.

Run with:

    uv run pytest tests/workflow_contracts
"""

from __future__ import annotations

import pathlib
import re
import typing as typ

import pytest
import yaml

#: A full lowercase commit SHA, which is the only reference an action pin may
#: carry: a tag or a branch can be moved under the repository's feet.
FULL_SHA = re.compile(r"[0-9a-f]{40}")

ROOT = pathlib.Path(__file__).resolve().parents[2]
WORKFLOW_DIR = ROOT / ".github" / "workflows"

#: The workflow that owns the trunk generation and the upload.
PUBLISHER = "coverage-main.yml"
PUBLISHER_JOB = "coverage-upload"
#: The lane that serves pull requests.
PR_WORKFLOW = "ci.yml"
PR_JOB = "lint-test"

#: Events that let a pull request's head decide what runs.
PULL_REQUEST_EVENTS = frozenset({"pull_request", "pull_request_target"})

#: What a CodeScene interaction looks like, wherever it is written. The
#: credential is named separately from the action because a lane can be given
#: it without calling the action, and a credential a pull request's head can
#: reach is what CV-005 is really about.
#: Markers matched against any scalar in the document. A credential can be
#: written anywhere, and a `cs-coverage` invocation can be buried in a shell
#: script, so neither is scoped to a key.
MARKERS: typ.Final[dict[str, str]] = {
    "a cs-coverage command": "cs-coverage",
    "the CodeScene credential": "CS_ACCESS_TOKEN",
}
#: The action marker is scoped to `uses` values, and matches any CodeScene
#: action rather than the one this repository happens to call today. Applied
#: to every scalar it would report a step named "check CodeScene coverage" as
#: an invocation, and scoped to one action reference it would miss a second
#: CodeScene action entirely; both readings are wrong in the direction that
#: matters.
ACTION_MARKER: typ.Final[str] = "codescene"
ACTION_DESCRIPTION: typ.Final[str] = "a CodeScene action"

#: Inputs that select what is measured. Publication differs between the two
#: lanes by design and is asserted separately.
SELECTION_INPUTS = ("output-path", "format", "pytest-workers", "with-ratchet")

#: Workflows allowed to trip a marker outside the publisher, each with the
#: reason and the single marker it may trip. Exempting a whole file would let
#: it acquire the credential too, which is the thing the scan exists to find;
#: the exemption is therefore per marker.
#:
#: `get-codescene-sha.yml` is dispatch-only and names CodeScene in a download
#: URL that happens to contain `cs-coverage`. It invokes no action and
#: receives no credential. Its output, the `CODESCENE_CLI_SHA256` repository
#: variable, no longer has a consumer: `installer-checksum` is the input that
#: read it, and the shared action now rejects that input. The workflow is kept
#: for the user to retire deliberately rather than deleted here.
SCAN_EXEMPT: typ.Final[dict[str, tuple[str, str]]] = {
    "get-codescene-sha.yml": (
        "workflow_dispatch only; names cs-coverage in a download URL",
        "a cs-coverage command",
    )
}


def _walk(value: object, path: str) -> typ.Iterator[tuple[str, str]]:
    """Yield every scalar in a parsed document with the path that reached it.

    Parameters
    ----------
    value : object
        A parsed YAML fragment.
    path : str
        The path taken to reach it.

    Yields
    ------
    tuple[str, str]
        The path and the scalar rendered as text.
    """
    match value:
        case dict():
            for key, entry in value.items():
                yield f"{path}.{key}", str(key)
                yield from _walk(entry, f"{path}.{key}")
        case list():
            for position, entry in enumerate(value):
                yield from _walk(entry, f"{path}[{position}]")
        case _:
            yield path, str(value)


def references_in(document: object, subject: str) -> list[str]:
    """Return every place a parsed document interacts with CodeScene.

    The whole document is walked, not a list of step keys. A credential can be
    declared at workflow scope, at job scope, on a step, or passed as an action
    input, and a scan reading only ``uses`` and ``run`` would miss three of
    those four.

    Pure, so the markers can be driven over documents written to carry one
    interaction each. Driven over this repository's workflows alone, a marker
    that had stopped matching would report a clean estate.

    Parameters
    ----------
    document : object
        A parsed workflow.
    subject : str
        What to name in the reported location.

    Returns
    -------
    list[str]
        One entry per interaction, naming what was found and where.
    """
    found = []
    for path, text in _walk(document, subject):
        lowered = text.lower()
        found.extend(
            f"{description} at {path}"
            for description, marker in MARKERS.items()
            if marker.lower() in lowered
        )
        if path.endswith(".uses") and ACTION_MARKER in lowered:
            found.append(f"{ACTION_DESCRIPTION} at {path}")
    return sorted(set(found))


def workflow(name: str) -> dict[object, object]:
    """Parse one workflow document.

    Parameters
    ----------
    name : str
        The workflow file name.

    Returns
    -------
    dict[object, object]
        The parsed document, keyed by object: YAML 1.1 reads the bare word
        ``on`` as the boolean ``True``.
    """
    document = yaml.safe_load((WORKFLOW_DIR / name).read_text(encoding="utf-8"))
    assert isinstance(document, dict), f"{name} must parse to a mapping"
    return document


def workflow_names() -> list[str]:
    """Return every workflow file name, sorted.

    Returns
    -------
    list[str]
        File names such as ``ci.yml``.
    """
    return sorted(
        path.name for path in WORKFLOW_DIR.iterdir() if path.suffix in {".yml", ".yaml"}
    )


def triggers(name: str) -> dict[str, object]:
    """Return a workflow's trigger block.

    Parameters
    ----------
    name : str
        The workflow file name.

    Returns
    -------
    dict[str, object]
        The declared events, keyed as strings.
    """
    document = workflow(name)
    declared = document.get("on", document.get(True))
    assert isinstance(declared, dict), f"{name} must declare an on: mapping"
    return {str(key): value for key, value in declared.items()}


def steps(name: str, job_name: str) -> list[dict[str, object]]:
    """Return one job's steps.

    Parameters
    ----------
    name : str
        The workflow file name.
    job_name : str
        The job key.

    Returns
    -------
    list[dict[str, object]]
        Each step mapping, in declaration order.
    """
    jobs = workflow(name).get("jobs")
    assert isinstance(jobs, dict), f"{name} must declare jobs"
    job = jobs.get(job_name)
    assert isinstance(job, dict), f"{name} must declare a {job_name!r} job"
    declared = job.get("steps")
    assert isinstance(declared, list), f"{name}:{job_name} must declare steps"
    return [step for step in declared if isinstance(step, dict)]


def declared_steps(name: str) -> typ.Iterator[dict[str, object]]:
    """Yield every step of every job in one workflow.

    A job that calls a reusable workflow declares no steps at all, so a walk
    that demanded a steps list would fail on `dependabot-automerge.yml` rather
    than on anything this contract is about.

    Parameters
    ----------
    name : str
        The workflow file name.

    Yields
    ------
    dict[str, object]
        Each declared step.
    """
    jobs = workflow(name).get("jobs")
    for job in jobs.values() if isinstance(jobs, dict) else ():
        yield from _job_steps(job)


def _job_steps(job: object) -> typ.Iterator[dict[str, object]]:
    """Yield one job's declared steps.

    Parameters
    ----------
    job : object
        A job mapping, or whatever YAML put there.

    Yields
    ------
    dict[str, object]
        Each declared step, none when the job declares no steps list.
    """
    declared = job.get("steps") if isinstance(job, dict) else None
    if not isinstance(declared, list):
        return
    yield from (step for step in declared if isinstance(step, dict))


def coverage_inputs(name: str, job_name: str) -> dict[str, object]:
    """Return the inputs of a job's coverage step.

    Parameters
    ----------
    name : str
        The workflow file name.
    job_name : str
        The job key.

    Returns
    -------
    dict[str, object]
        The ``with`` mapping of the step invoking ``generate-coverage``.
    """
    for step in steps(name, job_name):
        if "generate-coverage@" in str(step.get("uses", "")):
            inputs = step.get("with")
            assert isinstance(inputs, dict), f"{name}:{job_name} coverage needs inputs"
            return inputs
    pytest.fail(f"{name}:{job_name} must invoke generate-coverage")


def pull_request_workflows() -> list[str]:
    """Return every workflow a pull request's head can reach.

    Returns
    -------
    list[str]
        Sorted workflow file names.
    """
    return sorted(
        name for name in workflow_names() if PULL_REQUEST_EVENTS & set(triggers(name))
    )


#: One document per marker, each carrying exactly the interaction its marker
#: exists to find, and each written where a real workflow would put it: the
#: action on a step, the command in a shell script, the credential in job
#: scope. A marker is proved against its own document, not against the
#: repository's files, which pass a broken scanner just as readily.
MARKER_FIXTURES = {
    ACTION_DESCRIPTION: {
        "jobs": {
            "gate": {
                "steps": [
                    {
                        "uses": (
                            "leynos/shared-actions/.github/actions/"
                            "upload-codescene-coverage@" + "0" * 40
                        )
                    }
                ]
            }
        }
    },
    "a cs-coverage command": {
        "jobs": {
            "gate": {"steps": [{"run": "cs-coverage check --coverage-files c.xml"}]}
        }
    },
    "the CodeScene credential": {
        "jobs": {
            "gate": {
                "env": {"CS_ACCESS_TOKEN": "${{ secrets.CS_ACCESS_TOKEN }}"},
                "steps": [{"run": "true"}],
            }
        }
    },
}


#: Documents that name CodeScene without invoking it. Reporting either would
#: make the rule unusable: the first forbids an unrelated action whose name
#: happens to resemble it, the second forbids describing the rule in a step
#: name or a comment-like string.
NOT_AN_INVOCATION = {
    "a similarly named action": {
        "jobs": {
            "gate": {
                "steps": [
                    {
                        "uses": (
                            "leynos/shared-actions/.github/actions/"
                            "upload-coverage@" + "0" * 40
                        )
                    }
                ]
            }
        }
    },
    "the rule named in prose": {
        "jobs": {"gate": {"steps": [{"name": "No CodeScene here", "run": "make test"}]}}
    },
}
#: A second CodeScene action, which the marker must also find: scoping it to
#: the one reference this repository calls today would miss any other.
ANOTHER_CODESCENE_ACTION = {
    "jobs": {
        "gate": {
            "steps": [
                {
                    "uses": (
                        "leynos/shared-actions/.github/actions/"
                        "codescene-delta@" + "0" * 40
                    )
                }
            ]
        }
    }
}


@pytest.mark.parametrize("marker", [*sorted(MARKERS), ACTION_DESCRIPTION])
def test_each_marker_finds_its_own_interaction(marker: str) -> None:
    """Prove every marker separately.

    The scan clears a workflow by finding nothing, so a marker that had stopped
    matching would clear the very thing it exists to catch while every other
    marker kept the suite green.
    """
    found = references_in(MARKER_FIXTURES[marker], "fixture.yml")

    assert any(entry.startswith(marker) for entry in found), (
        f"{marker} must be recognized; the scan of its own fixture found {found}"
    )


@pytest.mark.parametrize("case", sorted(NOT_AN_INVOCATION))
def test_naming_codescene_is_not_invoking_it(case: str) -> None:
    """Refuse a marker that forbids describing the rule.

    A whole-document match on the action name reports a step called "No
    CodeScene here" as an invocation, and an action merely named
    `upload-coverage` escapes a marker scoped to one exact reference. Both
    readings make the rule unusable, in opposite directions.
    """
    found = references_in(NOT_AN_INVOCATION[case], "fixture.yml")
    actions = [entry for entry in found if entry.startswith(ACTION_DESCRIPTION)]

    assert not actions, f"{case} is not an invocation; the scan reported {actions}"


def test_a_second_codescene_action_is_found() -> None:
    """Find any CodeScene action, not the one called today.

    Scoping the marker to `upload-codescene-coverage` would clear a workflow
    that reached CodeScene through a different action in the same suite, which
    is the interaction the rule exists to forbid.
    """
    found = references_in(ANOTHER_CODESCENE_ACTION, "fixture.yml")

    assert any(entry.startswith(ACTION_DESCRIPTION) for entry in found), (
        f"a second CodeScene action must be found; the scan reported {found}"
    )


def test_the_upload_is_restricted_to_the_main_ref() -> None:
    """Publish only from `main`, whatever the event selected.

    `workflow_dispatch` can select any branch or tag. The push trigger's
    `branches: [main]` says nothing about a dispatch, so without a ref guard on
    the step itself a dispatch from a feature branch publishes that branch's
    coverage through the main-owned upload, and CodeScene records it as the
    trunk's. The shared action already holds the ratchet baseline to a push to
    `refs/heads/main`; this brings the report under the same rule.
    """
    uploads = [
        step
        for step in steps(PUBLISHER, PUBLISHER_JOB)
        if "upload-codescene-coverage@" in str(step.get("uses", ""))
    ]

    assert uploads, f"{PUBLISHER} must upload the trunk report"
    for step in uploads:
        guard = str(step.get("if", ""))
        assert "github.ref == 'refs/heads/main'" in guard, (
            f"{PUBLISHER}'s upload must publish only from main; its guard is {guard!r}"
        )


def test_the_publisher_serializes_its_trunk_generations() -> None:
    """Let one trunk generation finish before the next starts.

    The shared action saves a fresh ratchet-baseline cache per successful push
    and later runs restore the newest match. Two overlapping pushes to `main`
    would both publish, and the older commit finishing last would leave its
    baseline as the one every pull request is then measured against. Nothing
    is cancelled: a trunk generation that has started is the one that should
    finish.
    """
    declared = workflow(PUBLISHER).get("concurrency")

    assert isinstance(declared, dict), (
        f"{PUBLISHER} must declare a concurrency group; it declares {declared!r}"
    )
    assert declared.get("cancel-in-progress") is False, (
        f"{PUBLISHER} must not cancel a running trunk generation; it declares "
        f"cancel-in-progress={declared.get('cancel-in-progress')!r}"
    )
    assert "github.ref" in str(declared.get("group")), (
        f"{PUBLISHER}'s group must serialize per ref so two pushes to main "
        f"queue; it declares {declared.get('group')!r}"
    )


def test_a_workflow_free_of_codescene_is_cleared() -> None:
    """Prove the scan can clear as well as refuse.

    A scanner reporting an interaction in every document would pass its marker
    tests and fail every real workflow, which is the opposite failure and just
    as invisible from the rules below.
    """
    innocent = {"jobs": {"gate": {"steps": [{"run": "make test"}]}}}
    found = references_in(innocent, "fixture.yml")

    assert found == [], (
        "a workflow naming no CodeScene action, running no cs-coverage command "
        f"and carrying no credential must be cleared; found {found}"
    )


def test_the_scan_reaches_the_workflows_it_guards() -> None:
    """Refuse a vacuous traversal.

    A traversal finding nothing would report that no pull-request workflow
    contacts CodeScene while asserting nothing at all.
    """
    reachable = pull_request_workflows()

    assert PR_WORKFLOW in reachable, (
        f"the scan must reach {PR_WORKFLOW}; it reached {reachable}"
    )
    assert references_in(workflow(PUBLISHER), PUBLISHER), (
        f"{PUBLISHER} must contact CodeScene, and the scanner must say so"
    )


def test_no_pull_request_workflow_contacts_codescene() -> None:
    """Keep CodeScene out of everything a pull request can reach.

    Not merely out of the merge gate's coverage step: a credential on any lane
    a pull request's head can reach is the exposure, and an outage in any such
    lane is a block the change under review cannot clear.
    """
    offending = {}
    for name in pull_request_workflows():
        found = references_in(workflow(name), name)
        if found:
            offending[name] = found

    assert not offending, (
        "no workflow reachable from a pull request may name a CodeScene "
        "action, run cs-coverage, or carry CS_ACCESS_TOKEN; main owns every "
        f"CodeScene interaction (CV-005). Found: {offending}"
    )


def test_only_the_publisher_and_the_named_exemptions_name_codescene() -> None:
    """Name every workflow that mentions CodeScene at all.

    The rule above clears pull-request lanes. A dispatch-only workflow could
    still acquire the credential without it noticing, so the whole estate is
    accounted for and the exemptions carry their reasons.
    """
    naming = {
        name
        for name in workflow_names()
        if references_in(workflow(name), name) or "codescene" in name.lower()
    }

    assert naming == {PUBLISHER} | set(SCAN_EXEMPT), (
        "every workflow naming CodeScene must be the publisher or a recorded "
        f"exemption; found {sorted(naming)}"
    )


def test_an_exempt_workflow_trips_only_the_marker_it_is_exempt_for() -> None:
    """Hold each exemption to one marker.

    A file-wide exemption would let an exempt workflow acquire the credential
    or call the action, which is exactly what the scan exists to find. The
    exemption names the one marker the file may trip, and nothing else.
    """
    for name, (reason, allowed) in SCAN_EXEMPT.items():
        found = references_in(workflow(name), name)
        unexpected = [entry for entry in found if not entry.startswith(allowed)]
        assert not unexpected, (
            f"{name} is exempt only for {allowed!r} ({reason}); it also "
            f"carries {unexpected}"
        )


def test_no_workflow_outside_the_publisher_carries_the_credential() -> None:
    """Hold the credential to one workflow, whatever its triggers.

    The pull-request rule clears lanes a pull request can reach. This one
    admits no exemptions at all: a dispatch-only or scheduled workflow holding
    `CS_ACCESS_TOKEN` is a second place the credential can leak from, and the
    estate rule puts it in one.
    """
    marker = MARKERS["the CodeScene credential"]
    carrying = sorted(
        name
        for name in workflow_names()
        if name != PUBLISHER
        and any(
            marker.lower() in text.lower() for _, text in _walk(workflow(name), name)
        )
    )

    assert not carrying, (
        f"only {PUBLISHER} may carry {marker}; it is also in {carrying}"
    )


def test_the_publisher_is_not_reachable_from_a_pull_request() -> None:
    """Keep the workflow that holds the credential off pull-request events."""
    declared = set(triggers(PUBLISHER))

    assert not PULL_REQUEST_EVENTS & declared, (
        f"{PUBLISHER} holds the CodeScene credential, so it must not trigger "
        f"on a pull request; it declares {sorted(declared)}"
    )
    assert declared <= {"push", "workflow_dispatch"}, (
        f"{PUBLISHER} may trigger only on a push or a dispatch; it declares "
        f"{sorted(declared)}"
    )
    push = triggers(PUBLISHER)["push"]
    assert isinstance(push, dict), f"{PUBLISHER} must filter its push trigger"
    assert push.get("branches") == ["main"], (
        f"{PUBLISHER} must be restricted to pushes to main; got "
        f"{push.get('branches')!r}"
    )


def test_the_publisher_uploads_rather_than_checks() -> None:
    """Upload the trunk report; never gate on it from here.

    ``mode: check`` is the pull-request form, and the form that failed.
    """
    uploads = [
        step
        for step in steps(PUBLISHER, PUBLISHER_JOB)
        if "upload-codescene-coverage@" in str(step.get("uses", ""))
    ]

    assert uploads, f"{PUBLISHER} must upload the trunk report to CodeScene"
    for step in uploads:
        inputs = step.get("with")
        assert isinstance(inputs, dict), f"{PUBLISHER} upload must declare inputs"
        assert inputs.get("mode") == "upload", (
            f"{PUBLISHER} must upload, not {inputs.get('mode')!r}"
        )


def test_no_caller_passes_the_deprecated_installer_checksum() -> None:
    """Refuse the input the shared action now rejects.

    From shared-actions f68e8e2e the CodeScene CLI is pinned through a manifest
    and a non-empty ``installer-checksum`` fails the run outright.
    ``archive-checksum`` replaces it, so passing the old input is a red lane
    rather than a deprecation warning.
    """
    offending = [
        f"{name}: {path}"
        for name in workflow_names()
        for path, text in _walk(workflow(name), name)
        if path.endswith(".installer-checksum") and text
    ]

    assert not offending, (
        f"installer-checksum is rejected when non-empty; use archive-checksum: "
        f"{offending}"
    )


def test_every_shared_coverage_action_is_sha_pinned_to_one_revision() -> None:
    """Pin both shared actions to one full commit SHA.

    Two revisions would let the publisher be repinned by itself, and the
    baseline it wrote would then come from a different implementation than the
    one measuring the pull request compared against it.
    """
    used = {
        str(step.get("uses"))
        for name in workflow_names()
        for step in declared_steps(name)
        if "generate-coverage@" in str(step.get("uses", ""))
        or "upload-codescene-coverage@" in str(step.get("uses", ""))
    }
    revisions = {reference.rsplit("@", 1)[1] for reference in used}

    assert used, "the repository must invoke the shared coverage actions"
    unpinned = [
        reference
        for reference in sorted(used)
        if not FULL_SHA.fullmatch(reference.rsplit("@", 1)[1])
    ]
    assert not unpinned, (
        f"every shared coverage action must be pinned to a full lowercase "
        f"40-character SHA; found {unpinned}"
    )
    assert len(revisions) == 1, (
        f"both shared coverage actions must use one revision; found {revisions}"
    )


def test_the_pull_request_lane_declines_publication() -> None:
    """Keep report publication to the workflow that owns it.

    The action archives the report under a step of its own, which no scanner
    over this workflow's steps can see, so declining the archive explicitly is
    the only way the boundary is observable here.
    """
    inputs = coverage_inputs(PR_WORKFLOW, PR_JOB)

    assert inputs.get("publish-artefact") == "false", (
        f"{PR_WORKFLOW}:{PR_JOB} must decline the artefact; publication belongs "
        f"to {PUBLISHER}. It declares {inputs.get('publish-artefact')!r}"
    )


def test_the_publisher_publishes_the_report() -> None:
    """Leave the publisher on the action's default.

    Setting ``publish-artefact`` here as the pull-request lane does would leave
    the upload with no report to read.
    """
    inputs = coverage_inputs(PUBLISHER, PUBLISHER_JOB)

    assert "publish-artefact" not in inputs, (
        f"{PUBLISHER} must keep the action's default; it declares "
        f"publish-artefact={inputs.get('publish-artefact')!r}"
    )


@pytest.mark.parametrize("field", SELECTION_INPUTS)
def test_both_lanes_measure_the_same_thing(field: str) -> None:
    """Hold the trunk generation and the pull-request check to one selection.

    The ratchet compares a pull request's changed-line coverage against the
    baseline the trunk wrote. If the two lanes select differently, that
    comparison is between two different measurements and a pull request can
    fail or pass on the difference rather than on its own change.
    """
    gate = coverage_inputs(PR_WORKFLOW, PR_JOB)
    publisher = coverage_inputs(PUBLISHER, PUBLISHER_JOB)

    assert gate.get(field) == publisher.get(field), (
        f"{PR_WORKFLOW} selects {field}={gate.get(field)!r} and {PUBLISHER} "
        f"selects {publisher.get(field)!r}; the baseline would not be "
        "comparable with what the ratchet checks"
    )


def test_the_upload_reads_the_format_that_was_generated() -> None:
    """Upload the report in the format the generator wrote.

    The two are separate inputs on separate steps, so nothing else notices
    them disagreeing. CodeScene would be handed a cobertura document labelled
    as lcov, or the reverse, and the upload fails at parse time on the trunk
    rather than anywhere a pull request could see.
    """
    generated = coverage_inputs(PUBLISHER, PUBLISHER_JOB).get("format")
    uploads = [
        step
        for step in steps(PUBLISHER, PUBLISHER_JOB)
        if "upload-codescene-coverage@" in str(step.get("uses", ""))
    ]

    assert uploads, f"{PUBLISHER} must upload the trunk report"
    for step in uploads:
        inputs = step.get("with")
        assert isinstance(inputs, dict), f"{PUBLISHER} upload must declare inputs"
        assert inputs.get("format") == generated, (
            f"{PUBLISHER} generates {generated!r} and uploads {inputs.get('format')!r}"
        )


def test_the_selection_list_covers_every_input_both_lanes_declare() -> None:
    """Refuse a selection list that has fallen behind the workflows.

    The list is named, so an input added to both lanes and not to it would
    simply not be compared, and the two could then drift on it.
    """
    gate = set(coverage_inputs(PR_WORKFLOW, PR_JOB))
    publisher = set(coverage_inputs(PUBLISHER, PUBLISHER_JOB))
    shared = gate & publisher

    assert shared == set(SELECTION_INPUTS), (
        "every input both lanes declare must be compared; unlisted: "
        f"{sorted(shared - set(SELECTION_INPUTS))}, listed but absent: "
        f"{sorted(set(SELECTION_INPUTS) - shared)}"
    )
