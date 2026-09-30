"""Verify framework diagnostics omit short authentication payload secrets.

Exercise errors, logging, context display, and traces without weakening trusted
raw access. Run with ``uv run pytest tests/test_diagnostic_paths_unit.py``.
"""

from __future__ import annotations

import asyncio
import logging
import traceback
import typing as typ

import msgspec as ms
import pytest

from falcon_pachinko import DiagnosticSanitizer, WebSocketResource, dispatcher
from falcon_pachinko.dispatcher import HandlerInvocationContext
from falcon_pachinko.exceptions import HandlerSignatureError
from falcon_pachinko.handlers import HandlerInfo
from falcon_pachinko.hooks import HookContext, HookEvent
from falcon_pachinko.schema import validate_strict_payload
from falcon_pachinko.testing import TraceEvent, WebSocketSimulator
from falcon_pachinko.testing.client import WebSocketSession
from falcon_pachinko.utils import raise_unknown_fields
from tests._stubs import Hostile, RecordingWebSocket

if typ.TYPE_CHECKING:
    from websockets.client import WebSocketClientProtocol

CANARY = "CANARY-short-auth-61"


class Hello(ms.Struct, tag="client.hello"):
    """Authentication schema whose token has an intentionally invalid type."""

    token: int


class FallbackResource(WebSocketResource):
    """Retain the raw fallback value before raising an application error."""

    def __init__(self) -> None:
        self.raw: str | bytes | None = None

    async def on_unhandled(self, ws: object, message: str | bytes) -> None:
        """Make any retained vendor exception context observable."""
        self.raw = message
        msg = "application fallback failed"
        raise RuntimeError(msg)

    async def on_client_hello(self, ws: object, payload: Hello) -> None:
        """Provide a conventional handler for logging regression tests."""


@pytest.mark.parametrize("include_payload", [False, True])
def test_unknown_fields_omit_values(*, include_payload: bool) -> None:
    """Even explicitly sampled diagnostics must redact known secret keys."""
    with pytest.raises(ms.ValidationError) as caught:
        raise_unknown_fields(
            {"extra"},
            {"nested": [{"access-token": CANARY}]},
            include_payload=include_payload,
        )
    assert CANARY not in str(caught.value), "field errors must omit secret values"


def test_unknown_fields_bound_names_and_report_expected_schema() -> None:
    """Invalid names cannot inject log lines or produce unbounded errors."""
    with pytest.raises(ms.ValidationError) as caught:
        raise_unknown_fields({"x" * 1000, "bad\nname"}, expected_type=Hello)
    message = str(caught.value)
    assert len(message) < 400, "field names must be bounded before rendering"
    assert "Hello" in message, "validation metadata should identify the schema"
    assert "bad\nname" not in message, "invalid names must use a placeholder"


def test_unknown_fields_support_extended_sanitizer() -> None:
    """Applications can extend redaction in explicit diagnostic samples."""
    sanitizer = DiagnosticSanitizer(extra_sensitive_keys=frozenset({"tenant-key"}))
    with pytest.raises(ms.ValidationError) as caught:
        raise_unknown_fields(
            {"extra"},
            {"tenant_key": CANARY},
            include_payload=True,
            sanitizer=sanitizer,
        )
    assert CANARY not in str(caught.value), "custom sensitive fields must be redacted"


def test_strict_validation_reports_schema_without_values() -> None:
    """Schema validation forwards structural expected-type metadata."""
    with pytest.raises(ms.ValidationError) as caught:
        validate_strict_payload({"token": CANARY, "extra": CANARY}, Hello, strict=True)
    assert "Hello" in str(caught.value), "strict errors should identify expected type"
    assert CANARY not in str(caught.value), "strict errors must omit decoded values"


@pytest.mark.parametrize("path", ["schema", "envelope", "conversion"])
@pytest.mark.asyncio
async def test_fallback_does_not_retain_vendor_exception(path: str) -> None:
    """Fallback failures have no vendor chain while receiving identical raw data."""
    resource = FallbackResource()
    ws = RecordingWebSocket()
    raw = f'{{"token":"{CANARY}","type":"unknown"}}'.encode()
    match path:
        case "schema":
            resource.schema = Hello
            action = dispatcher.dispatch_with_schema(resource, ws, raw)
        case "envelope":
            raw = raw[:-1]
            action = dispatcher.dispatch_with_envelope(resource, ws, raw)
        case _:
            info = HandlerInfo(FallbackResource.on_client_hello, Hello)
            context = HandlerInvocationContext(
                resource, ws, raw, info, {"token": CANARY}
            )
            action = dispatcher.convert_and_invoke_handler(context)
    with pytest.raises(RuntimeError) as caught:
        await action
    assert resource.raw is raw, "fallback must retain its original raw argument"
    assert caught.value.__context__ is None, (
        "fallback errors must detach vendor context"
    )
    assert caught.value.__cause__ is None, "fallback errors must detach vendor cause"
    assert CANARY not in "".join(traceback.format_exception(caught.value)), (
        "formatted fallback failures must omit authentication secrets"
    )


def test_conventional_handler_debug_log_omits_exception_text(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Debug logging reports only handler and exception class metadata."""

    def fail_signature(_: object) -> typ.Never:
        raise HandlerSignatureError(CANARY)

    monkeypatch.setattr(dispatcher, "get_payload_type", fail_signature)
    with caplog.at_level(logging.DEBUG, logger=dispatcher.__name__):
        result = dispatcher.find_conventional_handler(
            FallbackResource(), "client.hello"
        )
    assert result is None, "invalid conventional handlers must be ignored"
    assert CANARY not in caplog.text, "debug logs must omit vendor exception text"
    assert "HandlerSignatureError" in caplog.text, "logs should retain exception class"


def test_context_representations_omit_values_and_hostile_objects() -> None:
    """Default representations disclose structure without invoking display hooks."""
    resource = FallbackResource()
    ws = RecordingWebSocket()
    raw = f'{{"token":"{CANARY}"}}'
    hook = HookContext(
        HookEvent.AFTER_RECEIVE,
        resource,
        resource,
        raw=raw,
        params={"token": CANARY},
        error=RuntimeError(CANARY),
    )
    invocation = HandlerInvocationContext(
        resource,
        ws,
        raw,
        HandlerInfo(FallbackResource.on_client_hello, Hello),
        {"token": CANARY},
    )
    assert CANARY not in repr([hook, invocation]), "context display must omit values"
    hook.req = typ.cast("typ.Any", Hostile())
    invocation.payload = Hostile()
    assert "Hostile" in repr([hook, invocation]), (
        "unsupported objects disclose only type"
    )
    assert hook.raw is raw, "hook raw values must remain unchanged"
    assert invocation.raw is raw, "invocation raw values must remain unchanged"


def test_trace_representations_and_summary_omit_values() -> None:
    """Trace payload storage remains trusted while default display is structural."""
    payload = {"token": CANARY}
    event = TraceEvent(0, "receive", "json", payload)
    assert CANARY not in repr([event]), "trace list display must omit payload values"
    assert CANARY not in repr(event.summary()), "trace summaries must omit values"
    assert event.payload is payload, "trace payloads must preserve the original object"
    event.payload = Hostile()
    assert "Hostile" in repr(event), "trace display must not invoke payload repr"


@pytest.mark.parametrize("binary", [False, True])
@pytest.mark.parametrize("typed", [False, True])
def test_client_decode_error_omits_frames_and_exception_chains(
    *, binary: bool, typed: bool
) -> None:
    """Short malformed authentication frames never enter client error output."""
    text = f'{{"token":"{CANARY}","type":"unknown"}}'
    raw = text.encode() if binary else text
    if not typed:
        raw = raw[:-1]
    session = WebSocketSession(
        typ.cast("WebSocketClientProtocol", object()), path="/", trace=[]
    )
    with pytest.raises(RuntimeError, match="Failed to decode JSON payload") as caught:
        session._decode_json_frame(raw, Hello if typed else None)
    assert CANARY not in str(caught.value), "client errors must omit frame values"
    assert caught.value.__cause__ is None, "client errors must not retain vendor cause"
    assert caught.value.__context__ is None, (
        "client errors must not retain vendor context"
    )
    assert CANARY not in "".join(traceback.format_exception(caught.value)), (
        "formatted client errors must omit raw frames"
    )


@pytest.mark.parametrize("binary", [False, True])
@pytest.mark.parametrize("typed", [False, True])
@pytest.mark.asyncio
async def test_simulator_decode_error_omits_vendor_values(
    *, binary: bool, typed: bool
) -> None:
    """Simulator decoding preserves exception types with sanitized error objects."""
    simulator = WebSocketSimulator()
    text = f'{{"token":"{CANARY}","type":"{CANARY}"}}'
    raw = text.encode() if binary else text
    if not typed:
        raw = raw[:-1]
    await simulator.push_message(raw, kind="bytes" if binary else "text")
    expected_error = ms.ValidationError if typed else ms.DecodeError
    with pytest.raises(expected_error, match="Failed to decode JSON payload") as caught:
        await simulator.receive_json(Hello if typed else None)
    assert CANARY not in str(caught.value), "simulator errors must omit vendor values"
    assert caught.value.__context__ is None, (
        "simulator errors must detach vendor context"
    )
    assert simulator.received_messages == [raw], (
        "recorded receive values must remain raw"
    )


@pytest.mark.asyncio
async def test_simulator_unsupported_frame_omits_hostile_representation() -> None:
    """Unsupported frames disclose type without invoking repr or str."""
    inbound: asyncio.Queue[object] = asyncio.Queue()
    await inbound.put(Hostile())
    simulator = WebSocketSimulator(inbound=inbound)
    with pytest.raises(TypeError, match="Hostile"):
        await simulator.receive_json()


@pytest.mark.asyncio
async def test_close_failure_trace_records_class_and_preserves_error() -> None:
    """Close traces omit error text but propagate the original application error."""
    error = RuntimeError(CANARY)

    class FailingConnection:
        """Expose a close operation that fails with sensitive application text."""

        async def close(self, *, code: int, reason: str) -> typ.Never:
            """Raise the original error for propagation assertions."""
            raise error

    trace: list[TraceEvent] = []
    session = WebSocketSession(
        typ.cast("WebSocketClientProtocol", FailingConnection()),
        path="/",
        trace=trace,
    )
    with pytest.raises(RuntimeError) as caught:
        await session.close(code=1001, reason="trusted application reason")
    assert caught.value is error, "close failures must propagate unchanged"
    assert trace[0].payload == {
        "code": 1001,
        "reason": "trusted application reason",
        "exception": "RuntimeError",
    }, "close trace payload must retain caller metadata and only the error class"
    assert CANARY not in repr(trace), "default trace display must omit error values"


@pytest.mark.asyncio
async def test_harness_decode_error_omits_vendor_values() -> None:
    """Outbound frame helpers must also sanitize vendor exception objects."""
    from falcon_pachinko import WebSocketRouter
    from falcon_pachinko._testing_harness import _OriginalWebSocket
    from falcon_pachinko.testing.harness import SimulatorConnection

    simulator = WebSocketSimulator()
    raw = f'{{"token":"{CANARY}","type":"{CANARY}"}}'
    await simulator.send_text(raw)
    connection = SimulatorConnection(
        "/",
        WebSocketRouter(),
        simulator,
        object(),
        _OriginalWebSocket(),
    )
    with pytest.raises(
        ms.ValidationError, match="Failed to decode JSON payload"
    ) as caught:
        connection.pop_sent_json(Hello)
    assert CANARY not in str(caught.value), (
        "outbound helper errors must omit vendor values"
    )
    assert caught.value.__context__ is None, (
        "outbound errors must detach vendor context"
    )


@pytest.mark.parametrize(
    ("info", "expected"),
    [
        (HandlerInfo(FallbackResource.on_client_hello, Hello), "Hello"),
        (HandlerInfo(FallbackResource.on_client_hello, None), "<omitted>"),
        (Hostile(), "<omitted>"),
    ],
)
def test_invocation_expected_type_label_is_non_reflective(
    info: object, expected: str
) -> None:
    """Expected-type metadata inspects only exact framework handler records."""
    assert dispatcher._expected_type_label(info) == expected, (
        "unsupported records must be omitted and supported schemas identified"
    )
