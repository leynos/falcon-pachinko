"""Tests for the optional schema-dispatch counter adapter."""

from __future__ import annotations

import contextlib
import logging
import typing as typ

import msgspec as ms
import msgspec.json as msjson
import pytest

from falcon_pachinko import (
    SCHEMA_DISPATCH_OUTCOMES,
    SchemaDispatchCounter,
    WebSocketLike,
    WebSocketResource,
    handles_message,
)
from falcon_pachinko.observability import Counter
from falcon_pachinko.unittests.helpers import DummyWS

_DISPATCHER_LOGGER = "falcon_pachinko.dispatcher"
_DISPATCH_MESSAGE = "Schema dispatch observation"

if typ.TYPE_CHECKING:  # pragma: no cover - type hints only
    import collections.abc as cabc


class Join(ms.Struct, tag="join"):
    """Message structure for join events."""

    room: str


class CounterResource(WebSocketResource):
    """Resource whose frames exercise the delivered and rejected paths."""

    schema = Join

    @handles_message("join")
    async def handle_join(self, ws: WebSocketLike, payload: Join) -> None:
        """Accept the frame, so dispatch records ``delivered``."""


@contextlib.contextmanager
def _counting(
    counter: SchemaDispatchCounter,
) -> cabc.Generator[SchemaDispatchCounter, None, None]:
    """Attach ``counter`` to the dispatcher logger at DEBUG for one test."""
    logger = logging.getLogger(_DISPATCHER_LOGGER)
    previous_level = logger.level
    logger.setLevel(logging.DEBUG)
    logger.addHandler(counter)
    try:
        yield counter
    finally:
        logger.removeHandler(counter)
        # ``NOTSET`` is restored as-is so the logger resumes inheriting.
        logger.setLevel(previous_level)


def _emit(outcome: object, *, logger_name: str = _DISPATCHER_LOGGER) -> None:
    """Emit one record shaped like a dispatcher observation."""
    logging.getLogger(logger_name).debug(_DISPATCH_MESSAGE, extra={"outcome": outcome})


def test_counter_name_is_a_documented_counter() -> None:
    """The adapter exports under the enum's schema-dispatch counter name."""
    counter = SchemaDispatchCounter()
    assert counter.counter_name is Counter.SCHEMA_DISPATCH_OUTCOMES, (
        "the adapter must export under the published counter name"
    )


@pytest.mark.asyncio
async def test_counter_counts_real_dispatch_outcomes() -> None:
    """Counts follow the decisions the dispatcher actually records."""
    resource = CounterResource()
    resource.bind_default_hook_manager()
    counter = SchemaDispatchCounter()

    with _counting(counter):
        await resource.dispatch(DummyWS(), msjson.encode(Join(room="lobby")))
        await resource.dispatch(DummyWS(), b"not json")

    assert counter.counts() == {"delivered": 1, "malformed": 1}, (
        "each dispatched frame must be counted under its decision"
    )
    assert counter.rejection_ratio() == pytest.approx(0.5), (
        "one rejection out of two frames is a ratio of one half"
    )


def test_counter_ignores_records_from_another_logger() -> None:
    """Only the dispatcher's own records are counted."""
    counter = SchemaDispatchCounter()
    with _counting(counter):
        _emit("delivered", logger_name="falcon_pachinko.router")

    assert not counter.counts(), "another logger's records must not be counted"


def test_counter_ignores_records_with_another_message() -> None:
    """A dispatcher record that is not a schema observation is ignored."""
    counter = SchemaDispatchCounter()
    with _counting(counter):
        logging.getLogger(_DISPATCHER_LOGGER).debug(
            "Operation observation", extra={"outcome": "delivered"}
        )

    assert not counter.counts(), "a non-observation record must not be counted"


@pytest.mark.parametrize("outcome", ["other", "CANARY-unknown-outcome", None])
def test_counter_ignores_unbounded_outcomes(outcome: object) -> None:
    """An outcome outside the fixed vocabulary cannot add a label."""
    counter = SchemaDispatchCounter()
    with _counting(counter):
        _emit(outcome)

    assert not counter.counts(), "an unbounded outcome must not add a label"


def test_rejection_ratio_without_frames_is_zero() -> None:
    """A fresh counter is safe to export before any traffic arrives."""
    assert SchemaDispatchCounter().rejection_ratio() == pytest.approx(0.0), (
        "an empty counter must report a zero ratio rather than raising"
    )


def test_rejection_ratio_counts_every_non_delivered_label() -> None:
    """Only ``delivered`` is accepted, so all other labels count as rejections."""
    counter = SchemaDispatchCounter()
    with _counting(counter):
        for outcome in ("delivered", "unknown_tag", "no_handler", "malformed"):
            _emit(outcome)

    expected = {"delivered": 1, "unknown_tag": 1, "no_handler": 1, "malformed": 1}
    assert counter.counts() == expected, "each distinct outcome must keep its own count"
    assert counter.rejection_ratio() == pytest.approx(0.75), (
        "three rejections out of four frames is a ratio of three quarters"
    )


def test_counter_labels_are_the_published_bounded_set() -> None:
    """Every counted label is published, so exporters can predeclare them."""
    counter = SchemaDispatchCounter()
    with _counting(counter):
        for outcome in sorted(SCHEMA_DISPATCH_OUTCOMES):
            _emit(outcome)

    assert set(counter.counts()) == set(SCHEMA_DISPATCH_OUTCOMES), (
        "every published label must be countable"
    )
