"""Private operation observations expose bounded metadata without secrets."""

from __future__ import annotations

import asyncio
import logging

import pytest

from falcon_pachinko._observability import _observe_operation

logger = logging.getLogger(__name__)


def test_success_records_a_completed_operation(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Application handlers can derive duration and success from DEBUG events."""
    with (
        caplog.at_level(logging.DEBUG, logger=logger.name),
        _observe_operation(logger, "publish"),
    ):
        pass

    assert [vars(record)["outcome"] for record in caplog.records] == [
        "started",
        "succeeded",
    ], "successful operations must have ordered start and completion events"
    assert all(vars(record)["operation"] == "publish" for record in caplog.records), (
        "both events must identify the observed operation"
    )
    assert all(record.levelno == logging.DEBUG for record in caplog.records), (
        "normal observations must remain opt-in DEBUG events"
    )
    assert all(vars(record)["duration_seconds"] >= 0 for record in caplog.records), (
        "operation durations must be nonnegative"
    )
    assert all(vars(record)["exception_type"] is None for record in caplog.records), (
        "successful operations must not report an exception type"
    )


@pytest.mark.parametrize(
    ("error", "outcome", "exception_type"),
    [
        pytest.param(
            RuntimeError("private-payload"), "failed", "RuntimeError", id="failure"
        ),
        pytest.param(
            asyncio.CancelledError("private-payload"),
            "cancelled",
            "CancelledError",
            id="cancellation",
        ),
    ],
)
def test_failure_preserves_exception_and_omits_secrets(
    caplog: pytest.LogCaptureFixture,
    error: BaseException,
    outcome: str,
    exception_type: str,
) -> None:
    """Failure metadata contains neither exception text nor reflected objects."""
    with (
        caplog.at_level(logging.DEBUG, logger=logger.name),
        pytest.raises(type(error)) as caught,
        _observe_operation(logger, "deliver"),
    ):
        raise error

    assert caught.value is error, "observation must preserve the exact exception"
    assert [vars(record)["outcome"] for record in caplog.records] == [
        "started",
        outcome,
    ], "completion must distinguish failure from cancellation"
    completed = caplog.records[-1]
    assert vars(completed)["exception_type"] == exception_type, (
        "failure metadata must contain the safe exception type"
    )
    assert vars(completed)["duration_seconds"] >= 0, (
        "failure duration must be nonnegative"
    )
    assert all(record.exc_info is None for record in caplog.records), (
        "observations must omit tracebacks and exception objects"
    )
    assert all(
        "private-payload" not in repr(vars(record)) for record in caplog.records
    ), "exception text must not enter observation records"


def test_unrecognised_operation_is_not_a_metric_label(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Accidental caller-provided labels cannot leak identifiers into records."""
    with (
        caplog.at_level(logging.DEBUG, logger=logger.name),
        _observe_operation(logger, "private-connection-id"),
    ):
        pass

    assert all(vars(record)["operation"] == "other" for record in caplog.records), (
        "unrecognized operations must use the fixed fallback label"
    )
    assert all(
        "private-connection-id" not in repr(vars(record)) for record in caplog.records
    ), "caller-provided identifiers must not enter observation records"


def test_failure_only_observation_omits_normal_frame_events(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Per-frame scopes emit a single failure event and no normal traffic."""
    msg = "private-payload"
    with caplog.at_level(logging.DEBUG, logger=logger.name):
        with _observe_operation(logger, "decode", emit_success=False):
            pass
        assert not caplog.records, "successful frames must emit no normal events"
        with (
            pytest.raises(ValueError, match="private-payload"),
            _observe_operation(logger, "decode", emit_success=False),
        ):
            raise ValueError(msg)

    assert len(caplog.records) == 1, "a failed frame must emit one completion event"
    assert vars(caplog.records[0])["operation"] == "decode", (
        "malformed frames must identify the decode operation"
    )
    assert vars(caplog.records[0])["outcome"] == "failed", (
        "decode errors must report failure"
    )
    assert "private-payload" not in repr(vars(caplog.records[0])), (
        "failed frame observations must omit exception text"
    )
