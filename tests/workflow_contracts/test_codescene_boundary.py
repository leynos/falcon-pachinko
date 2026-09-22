"""CV-005: nothing a pull request can run contacts CodeScene.

A pull-request lane measures coverage for its own ratchet and does nothing else
with it. It carries no CodeScene action, no ``cs-coverage`` command, no call to
the service's host and no ``CS_ACCESS_TOKEN``, and forwards no secrets
wholesale. The subject is every workflow a pull request can run, which is a
closure through same-repository calls rather than a trigger list.

The separation is not tidiness. Between 2026-09-16 and 2026-09-18 an unpinned
``cs-coverage`` could not parse its own cobertura output, and because the check
ran inside the merge gate every pull request in this repository was blocked on
a step with nothing to say about the change under review. A pull-request lane
that cannot contact CodeScene cannot be stopped by CodeScene.

Run with:

    uv run pytest tests/workflow_contracts
"""

from __future__ import annotations

import pytest

from .codescene_scan import (
    ACTION_DESCRIPTION,
    DEPRECATED_DIGEST_VARIABLE,
    INHERITED_SECRETS,
    MARKERS,
    PR_WORKFLOW,
    PUBLISHER,
    SCAN_EXEMPT,
    _walk,
    pull_request_workflows,
    references_in,
)
from .workflow_support import REPOSITORY

#: One document per marker, each carrying exactly the interaction its marker
#: exists to find, and each written where a real workflow would put it: the
#: action on a step, the command and the host in a shell script, the
#: credential in job scope, and the wholesale forward on a calling job. A
#: marker is proved against its own document, not against the repository's
#: files, which pass a broken scanner just as readily.
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
    "the CodeScene service host": {
        "jobs": {
            "gate": {"steps": [{"run": "curl https://api.codescene.io/v2/projects"}]}
        }
    },
    INHERITED_SECRETS: {
        "jobs": {
            "gate": {
                "uses": "leynos/shared-actions/.github/workflows/x.yml@" + "0" * 40,
                "secrets": "inherit",
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


def test_every_marker_has_a_document_of_its_own() -> None:
    """Refuse a marker that no fixture proves.

    The rule below is parametrized over the fixtures, so a marker added
    without one would simply not be proved, and the suite would stay green.
    """
    expected = {*MARKERS, ACTION_DESCRIPTION, INHERITED_SECRETS}

    assert set(MARKER_FIXTURES) == expected, (
        f"every marker needs its own fixture; fixtures {sorted(MARKER_FIXTURES)}"
    )


@pytest.mark.parametrize("marker", sorted(MARKER_FIXTURES))
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
    reachable = pull_request_workflows(REPOSITORY)

    assert PR_WORKFLOW in reachable, (
        f"the scan must reach {PR_WORKFLOW}; it reached {reachable}"
    )
    assert references_in(REPOSITORY.document(PUBLISHER), PUBLISHER), (
        f"{PUBLISHER} must contact CodeScene, and the scanner must say so"
    )


def test_no_pull_request_workflow_contacts_codescene() -> None:
    """Keep CodeScene out of everything a pull request can reach.

    Not merely out of the merge gate's coverage step: a credential on any lane
    a pull request's head can reach is the exposure, and an outage in any such
    lane is a block the change under review cannot clear.
    """
    offending = {}
    for name in pull_request_workflows(REPOSITORY):
        found = references_in(REPOSITORY.document(name), name)
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
        for name in REPOSITORY.names()
        if references_in(REPOSITORY.document(name), name) or "codescene" in name.lower()
    }

    assert naming == {PUBLISHER} | set(SCAN_EXEMPT), (
        "every workflow naming CodeScene must be the publisher or a recorded "
        f"exemption; found {sorted(naming)}"
    )


def test_no_workflow_reads_the_deprecated_cli_digest() -> None:
    """Leave no workflow maintaining or reading a value nothing consumes.

    `CODESCENE_CLI_SHA256` held the digest of the CodeScene installer script,
    and `installer-checksum` was its only consumer. From shared-actions
    `f68e8e2e` that input is rejected when non-empty and the CLI is pinned
    through the action's own manifest instead. A workflow still reading the
    variable therefore feeds a rejected input, which fails only when the action
    runs; one still refreshing it maintains a value nothing reads, which never
    fails at all. Neither is visible without this rule.
    """
    offending = sorted(
        f"{name}: {path}"
        for name in REPOSITORY.names()
        for path, text in _walk(REPOSITORY.document(name), name)
        if DEPRECATED_DIGEST_VARIABLE in text
    )

    assert not offending, (
        f"no workflow may read or refresh {DEPRECATED_DIGEST_VARIABLE}; the "
        f"shared action pins the CLI through its own manifest: {offending}"
    )


def test_an_exempt_workflow_trips_only_the_marker_it_is_exempt_for() -> None:
    """Hold each exemption to one marker.

    A file-wide exemption would let an exempt workflow acquire the credential
    or call the action, which is exactly what the scan exists to find. The
    exemption names the one marker the file may trip, and nothing else.
    """
    for name, (reason, allowed) in SCAN_EXEMPT.items():
        found = references_in(REPOSITORY.document(name), name)
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
        for name in REPOSITORY.names()
        if name != PUBLISHER
        and any(
            marker.lower() in text.lower()
            for _, text in _walk(REPOSITORY.document(name), name)
        )
    )

    assert not carrying, (
        f"only {PUBLISHER} may carry {marker}; it is also in {carrying}"
    )
