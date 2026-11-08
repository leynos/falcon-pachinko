"""Delivery-time failure tests for schema-dispatch observations.

A frame's observation is emitted at the payload decision, before delivery, so
these tests drive the paths where delivery never completes: a handler that
raises, a handler that is cancelled, and a rejection fallback that raises.
"""

from __future__ import annotations

import asyncio
import dataclasses as dc
import logging
import typing as typ

import msgspec as ms
import msgspec.json as msjson
import pytest

from falcon_pachinko import WebSocketLike, WebSocketResource, handles_message
from falcon_pachinko.unittests.helpers import DummyWS

if typ.TYPE_CHECKING:  # pragma: no cover - type hints only
    import collections.abc as cabc

# The attributes ``logging`` adds to every record, plus the ones a formatter
# adds later. Anything outside this set arrived through ``extra`` and is
# therefore framework-chosen metadata.
_LOG_RECORD_KEYS = frozenset(
    vars(logging.LogRecord("", logging.DEBUG, "", 0, "", (), None))
) | frozenset({"message", "asctime"})


class Join(ms.Struct, tag="join"):
    """Message structure for join events."""

    room: str


class RaisingResource(WebSocketResource):
    """Resource whose handler fails after the frame is delivered."""

    schema = Join

    @handles_message("join")
    async def handle_join(self, ws: WebSocketLike, payload: Join) -> None:
        """Fail so the dispatch observation can be checked."""
        msg = "handler exploded"
        raise RuntimeError(msg)


class CancellingResource(WebSocketResource):
    """Resource whose handler cancels the dispatch before it completes."""

    schema = Join

    @handles_message("join")
    async def handle_join(self, ws: WebSocketLike, payload: Join) -> None:
        """Cancel, as a session shutdown during a handler would."""
        raise asyncio.CancelledError


class RaisingFallbackResource(WebSocketResource):
    """Resource whose rejection fallback fails."""

    schema = Join

    @handles_message("join")
    async def handle_join(self, ws: WebSocketLike, payload: Join) -> None:
        """Never runs; the frame below is rejected before delivery."""

    async def on_unhandled(self, ws: WebSocketLike, message: str | bytes) -> None:
        """Fail so a rejection that cannot be delivered is still observed."""
        msg = "fallback exploded"
        raise RuntimeError(msg)


def _schema_observations(
    caplog: pytest.LogCaptureFixture,
) -> list[tuple[object, object, object]]:
    """Return the bounded fields of each schema-dispatch observation."""
    return [
        (
            record.__dict__.get("outcome"),
            record.__dict__.get("handler_mode"),
            record.__dict__.get("strict"),
        )
        for record in caplog.records
        if record.getMessage() == "Schema dispatch observation"
    ]


def _assert_bounded_observations(
    caplog: pytest.LogCaptureFixture, *forbidden: str
) -> None:
    """Assert observations carry only permitted fields and no frame values."""
    # ``caplog.text`` renders only the formatted message, so a value smuggled
    # through ``extra`` would pass that check. Inspect every record's full
    # ``__dict__`` instead, which is where ``extra`` fields land.
    rendered = " ".join(repr(record.__dict__) for record in caplog.records)
    for value in forbidden:
        assert value not in rendered, f"{value!r} must not be logged"
    permitted = {
        "operation",
        "outcome",
        "resource_type",
        "handler_mode",
        "strict",
    }
    for record in caplog.records:
        if record.getMessage() != "Schema dispatch observation":
            continue
        assert set(record.__dict__) & permitted == permitted, (
            "each observation must carry the documented structured fields"
        )
        assert not set(record.__dict__) - permitted - _LOG_RECORD_KEYS, (
            "an observation must not carry undocumented fields"
        )


def _plain_frame(room: str) -> bytes:
    """Encode a frame that decodes as a valid ``Join``."""
    return msjson.encode(Join(room=room))


def _extra_field_frame(room: str) -> bytes:
    """Encode a ``Join`` frame with an undeclared field, which strict rejects."""
    return msjson.encode({"type": "join", "room": room, "extra": 1})


@dc.dataclass(frozen=True, slots=True)
class _DeliveryCase:
    """One scenario in which delivery fails after the payload decision."""

    resource_type: type[WebSocketResource]
    error: type[BaseException]
    match: str | None
    outcome: str
    frame_for: cabc.Callable[[str], bytes]
    room: str


@pytest.mark.parametrize(
    "case",
    [
        pytest.param(
            _DeliveryCase(
                RaisingResource,
                RuntimeError,
                "handler exploded",
                "delivered",
                _plain_frame,
                "CANARY-raising-room",
            ),
            id="raising-handler",
        ),
        pytest.param(
            _DeliveryCase(
                CancellingResource,
                asyncio.CancelledError,
                None,
                "delivered",
                _plain_frame,
                "CANARY-cancelled-room",
            ),
            id="cancelled-handler",
        ),
        pytest.param(
            _DeliveryCase(
                RaisingFallbackResource,
                RuntimeError,
                "fallback exploded",
                "rejected_fields",
                _extra_field_frame,
                "CANARY-fallback-room",
            ),
            id="raising-fallback",
        ),
    ],
)
@pytest.mark.asyncio
async def test_schema_dispatch_keeps_its_decision_when_delivery_fails(
    caplog: pytest.LogCaptureFixture, case: _DeliveryCase
) -> None:
    """Delivery failures keep the decision the frame was observed under.

    ``CancelledError`` is a ``BaseException``, so an observation emitted only
    after delivery would be skipped entirely for a cancelled handler. A raising
    fallback must also keep reporting its rejection, not a handler failure.
    """
    caplog.set_level(logging.DEBUG, logger="falcon_pachinko.dispatcher")
    resource = case.resource_type()
    resource.bind_default_hook_manager()
    raw = case.frame_for(case.room)
    with pytest.raises(case.error, match=case.match):
        await resource.dispatch(DummyWS(), raw)
    assert _schema_observations(caplog) == [(case.outcome, "registered", True)], (
        "the observation records the payload decision, not the delivery failure"
    )
    _assert_bounded_observations(caplog, case.room, raw.decode())
