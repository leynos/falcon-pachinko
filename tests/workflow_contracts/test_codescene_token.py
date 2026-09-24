"""CV-005: how the publisher holds the CodeScene token.

The upload action is composite. It binds the token itself from its
``access-token`` input and hands a step's ``env`` to its nested
upload-artifact and cache steps, so a token bound in any ``env`` travels
further than the one call that needs it. The publisher therefore learns
whether the secret exists from a check step whose one command GitHub evaluates
before the shell starts, gates the upload on that step's output, and passes the
secret straight to the input.

A guard written as ``env.CS_ACCESS_TOKEN != ''`` does not survive this: with
the binding deleted the guard is simply false, and the upload skips forever
without a failure anywhere. Hence the binding is asserted positively here.

Run with:

    uv run pytest tests/workflow_contracts
"""

from __future__ import annotations

from .codescene_scan import (
    CREDENTIAL_CHECK_COMMAND,
    CREDENTIAL_CHECK_ID,
    CREDENTIAL_OUTPUT,
    PUBLISHER,
    PUBLISHER_JOB,
    UPLOAD_ACTION,
    _walk,
    invokes,
    steps,
)
from .guard_conditions import conjuncts
from .workflow_support import REPOSITORY

CREDENTIAL_NAME = "CS_ACCESS_TOKEN"
CREDENTIAL_INPUT = "${{ secrets.CS_ACCESS_TOKEN }}"


def test_the_token_check_is_one_exact_command() -> None:
    """Require the check step, with its command alone and nothing around it.

    Deleting the step leaves the upload's guard reading an output nobody
    writes, so the upload skips forever. An ``if:`` would let ``false && X``
    carry the command without running it, and an ``env`` would bind the secret
    the step exists not to bind.
    """
    checks = [
        step
        for step in steps(REPOSITORY, PUBLISHER, PUBLISHER_JOB)
        if step.get("id") == CREDENTIAL_CHECK_ID
    ]

    assert len(checks) == 1, (
        f"{PUBLISHER}:{PUBLISHER_JOB} must declare one step with id "
        f"{CREDENTIAL_CHECK_ID!r}; found {len(checks)}"
    )
    check = checks[0]
    assert check.get("run") == CREDENTIAL_CHECK_COMMAND, (
        f"the token check must run exactly {CREDENTIAL_CHECK_COMMAND!r}; it runs "
        f"{check.get('run')!r}"
    )
    assert set(check) <= {"name", "id", "run"}, (
        f"the token check may carry only a name, its id and its command; it "
        f"also declares {sorted(set(check) - {'name', 'id', 'run'})}"
    )


def test_the_upload_consumes_the_check_and_takes_the_secret_directly() -> None:
    """Gate the upload on the check's output and pass the secret as the input."""
    uploads = [
        step
        for step in steps(REPOSITORY, PUBLISHER, PUBLISHER_JOB)
        if invokes(step, UPLOAD_ACTION)
    ]

    assert len(uploads) == 1, f"{PUBLISHER} must upload exactly once"
    upload = uploads[0]
    expected = f"{CREDENTIAL_OUTPUT} == 'true'"
    assert expected in conjuncts(str(upload.get("if", ""))), (
        f"the upload must carry {expected!r} as a conjunct; its guard is "
        f"{upload.get('if')!r}"
    )
    inputs = upload.get("with")
    assert isinstance(inputs, dict), f"{PUBLISHER} upload must declare inputs"
    assert inputs.get("access-token") == CREDENTIAL_INPUT, (
        f"the upload must pass {CREDENTIAL_INPUT!r} to access-token; it passes "
        f"{inputs.get('access-token')!r}"
    )


def test_no_env_in_the_publisher_carries_the_token() -> None:
    """Refuse the token in a workflow, job or step ``env``, under any name.

    The text of every scalar beneath an ``env`` is searched, so renaming the
    variable does not hide the secret it is bound to.
    """
    offending = sorted(
        path
        for path, text in _walk(REPOSITORY.document(PUBLISHER), PUBLISHER)
        if ".env." in path and CREDENTIAL_NAME in text
    )

    assert not offending, (
        f"{PUBLISHER} must bind {CREDENTIAL_NAME} in no env; the composite uploader "
        f"hands a step's env to its nested steps: {offending}"
    )
