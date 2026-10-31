"""Envelope payload validation for both handler selection modes."""

import logging

import msgspec as ms
import pytest

from falcon_pachinko import WebSocketLike, WebSocketResource, handles_message
from falcon_pachinko.unittests.helpers import DummyWS


def _rejection_records(
    caplog: pytest.LogCaptureFixture,
) -> list[logging.LogRecord]:
    """Return the dispatch rejection observations captured so far."""
    return [
        record
        for record in caplog.records
        if getattr(record, "outcome", None) == "rejected"
    ]


class Payload(ms.Struct):
    """Structured payload used to exercise conversion."""

    value: int


class RecordingResource(WebSocketResource):
    """Record selected handlers and rejected frames."""

    def __init__(self) -> None:
        self.seen: list[object] = []
        self.fallback: list[str | bytes] = []

    async def on_unhandled(self, ws: WebSocketLike, message: str | bytes) -> None:
        """Record the frame the dispatcher rejected."""
        self.fallback.append(message)

    async def on_integer(self, ws: WebSocketLike, payload: int) -> None:
        """Record a required integer payload."""
        self.seen.append(payload)

    async def on_struct(self, ws: WebSocketLike, payload: Payload) -> None:
        """Record a converted struct payload."""
        self.seen.append(payload)

    async def on_optional(self, ws: WebSocketLike, payload: int | None) -> None:
        """Record an optional payload, for which ``None`` is valid."""
        self.seen.append(payload)

    async def on_defaulted(self, ws: WebSocketLike, payload: int = 7) -> None:
        """Record a payload whose parameter declares a default."""
        self.seen.append(payload)

    @handles_message("decorated_integer")
    async def decorated_integer(self, ws: WebSocketLike, payload: int) -> None:
        """Record a required integer payload selected by decorator."""
        self.seen.append(payload)

    @handles_message("decorated_struct")
    async def decorated_struct(self, ws: WebSocketLike, payload: Payload) -> None:
        """Record a converted struct payload selected by decorator."""
        self.seen.append(payload)

    @handles_message("decorated_optional")
    async def decorated_optional(self, ws: WebSocketLike, payload: int | None) -> None:
        """Record an optional payload selected by decorator."""
        self.seen.append(payload)

    @handles_message("decorated_defaulted")
    async def decorated_defaulted(self, ws: WebSocketLike, payload: int = 7) -> None:
        """Record a defaulted payload selected by decorator."""
        self.seen.append(payload)

    @handles_message("lenient", strict=False)
    async def lenient(self, ws: WebSocketLike, payload: int) -> None:
        """Record a payload converted with ``strict=False``."""
        self.seen.append(payload)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "tag",
    [
        "integer",
        "struct",
        "defaulted",
        "decorated_integer",
        "decorated_struct",
        "decorated_defaulted",
        "lenient",
    ],
)
@pytest.mark.parametrize("suffix", ["", ', "payload": null'])
async def test_missing_payload_rejects_required_types(
    tag: str, suffix: str, caplog: pytest.LogCaptureFixture
) -> None:
    """Check the envelope contract for this handler and payload."""
    caplog.set_level(logging.DEBUG, logger="falcon_pachinko.dispatcher")
    resource = RecordingResource()
    resource.bind_default_hook_manager()
    raw = f'{{"type": "{tag}"{suffix}}}'
    await resource.dispatch(DummyWS(), raw)
    assert resource.fallback == [raw], "required payloads must be rejected"
    assert not resource.seen, "rejected payloads must not reach handlers"

    records = _rejection_records(caplog)
    assert len(records) == 1, f"exactly one rejection signal expected: {records}"
    record = records[0]
    # Custom ``extra`` fields are not attributes ``LogRecord`` declares, so
    # they are read from its instance dictionary.
    fields = vars(record)
    assert record.name == "falcon_pachinko.dispatcher", "signal source"
    assert record.levelno == logging.DEBUG, "the signal must stay at DEBUG"
    assert fields["operation"] == "dispatch", "the operation must be dispatch"
    assert fields["outcome"] == "rejected", "the outcome must be rejected"
    # The instrumentation emits a fixed literal, so exact equality is safe.
    assert fields["duration_seconds"] == 0, "the literal duration is zero"
    assert fields["exception_type"] == "ValidationError", "bounded type name"
    expected_type = "Payload" if "struct" in tag else "int"
    assert fields["expected_type"] == expected_type, "the declared payload type"
    rendered = record.getMessage() + repr(fields)
    assert raw not in rendered, "the raw frame must not leak into the signal"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tag", "suffix", "expected"),
    [
        ("optional", "", None),
        ("optional", ', "payload": null', None),
        ("decorated_optional", "", None),
        ("decorated_optional", ', "payload": null', None),
        ("integer", ', "payload": 5', 5),
        ("decorated_integer", ', "payload": 5', 5),
    ],
)
async def test_valid_payload_reaches_handler(
    tag: str, suffix: str, expected: object, caplog: pytest.LogCaptureFixture
) -> None:
    """Check the envelope contract for this handler and payload."""
    caplog.set_level(logging.DEBUG, logger="falcon_pachinko.dispatcher")
    resource = RecordingResource()
    resource.bind_default_hook_manager()
    await resource.dispatch(DummyWS(), f'{{"type": "{tag}"{suffix}}}')
    assert resource.seen == [expected], "valid payloads must reach handlers"
    assert not resource.fallback, "valid payloads must not be rejected"
    assert not _rejection_records(caplog), "accepted payloads must stay silent"


@pytest.mark.asyncio
async def test_rejection_signal_omits_peer_payload(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A rejected conversion reports its shape without peer-supplied values."""
    caplog.set_level(logging.DEBUG, logger="falcon_pachinko.dispatcher")
    resource = RecordingResource()
    resource.bind_default_hook_manager()
    sentinel = "CANARY-payload-secret"
    raw = f'{{"type": "integer", "payload": "{sentinel}"}}'
    await resource.dispatch(DummyWS(), raw)
    assert resource.fallback == [raw], "invalid payloads must be rejected"
    records = _rejection_records(caplog)
    assert len(records) == 1, f"exactly one rejection signal expected: {records}"
    rendered = records[0].getMessage() + repr(vars(records[0]))
    assert sentinel not in rendered, "peer payload values must not leak"
    assert raw not in rendered, "the raw frame must not leak"
