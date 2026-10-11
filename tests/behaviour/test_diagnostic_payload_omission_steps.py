"""Exercise payload omission and explicit local samples through public helpers.

Run with ``uv run pytest tests/behaviour/test_diagnostic_payload_omission_steps.py``.
"""

from __future__ import annotations

import asyncio
import dataclasses as dc
import traceback

import msgspec as ms
from pytest_bdd import given, scenario, then, when

from falcon_pachinko import DiagnosticSanitizer
from falcon_pachinko.testing import TraceEvent, WebSocketSimulator

CANARY = "CANARY-bdd-auth-61"


@dc.dataclass(slots=True)
class DiagnosticScenario:
    """Retain scenario state within an explicitly trusted test boundary."""

    simulator: WebSocketSimulator = dc.field(default_factory=WebSocketSimulator)
    raw: str = ""
    error: ms.DecodeError | None = None
    sample: object = None
    formatted: str = ""


@scenario(
    "diagnostic_payload_omission.feature", "malformed post-accept authentication frame"
)
def test_malformed_authentication_frame() -> None:
    """Register the post-accept authentication regression scenario."""


@scenario("diagnostic_payload_omission.feature", "explicitly sanitized local sample")
def test_explicit_sanitized_sample() -> None:
    """Register the application-extended redaction scenario."""


@given(
    "an accepted websocket with a short client.hello token frame",
    target_fixture="context",
)
def given_accepted_authentication_frame() -> DiagnosticScenario:
    """Accept a connection and enqueue a short malformed token frame."""
    context = DiagnosticScenario(raw=f'{{"token":"{CANARY}","type":"client.hello"')

    async def prepare() -> None:
        await context.simulator.accept()
        await context.simulator.push_text(context.raw)

    asyncio.run(prepare())
    return context


@when("the malformed authentication frame is decoded")
def when_decode_authentication(context: DiagnosticScenario) -> None:
    """Retain the sanitized framework error for subsequent diagnostic checks."""
    try:
        asyncio.run(context.simulator.receive_json())
    except ms.DecodeError as error:
        context.error = error


@then("the token is absent from diagnostic errors and trace summaries")
def then_safe_diagnostics(context: DiagnosticScenario) -> None:
    """Check exception objects, formatted chains, trace display, and summaries."""
    assert context.error is not None, "the malformed frame must fail decoding"
    event = TraceEvent(0, "receive", "text", context.raw)
    output = "\n".join([
        str(context.error),
        repr(context.error),
        "".join(traceback.format_exception(context.error)),
        repr([event]),
        repr(event.summary()),
    ])
    assert CANARY not in output, "default diagnostics must omit authentication tokens"
    assert context.error.__context__ is None, (
        "vendor error objects must not remain chained"
    )


@then("trusted raw access retains the original authentication frame")
def then_trusted_raw(context: DiagnosticScenario) -> None:
    """Confirm raw receive storage is preserved at the application boundary."""
    assert context.simulator.accepted, "authentication messages must follow acceptance"
    assert context.simulator.received_messages == [context.raw], (
        "trusted raw frames must remain original"
    )


@given(
    "a local diagnostic sample with nested tokens and an application key",
    target_fixture="context",
)
def given_sample() -> DiagnosticScenario:
    """Nest sensitive fields in lists and tuples with a custom application key."""
    return DiagnosticScenario(
        sample={
            "items": [{"ACCESS-TOKEN": CANARY}, ({"Tenant.Key": CANARY},)],
            "status": "ready",
        }
    )


@when("an explicitly configured sanitizer formats the sample")
def when_format_sample(context: DiagnosticScenario) -> None:
    """Opt in explicitly and extend the application's sensitive-key fragments."""
    sanitizer = DiagnosticSanitizer(extra_sensitive_keys=frozenset({"tenant-key"}))
    context.formatted = sanitizer.format_sample(context.sample)


@then("sensitive values are redacted and ordinary values remain visible")
def then_redacted_sample(context: DiagnosticScenario) -> None:
    """Check redaction independently of the precise sample text layout."""
    assert CANARY not in context.formatted, (
        "nested and application-specific keys must redact"
    )
    assert all(marker in context.formatted for marker in ("<redacted>", "ready")), (
        "explicit samples must show redaction markers and ordinary values"
    )
