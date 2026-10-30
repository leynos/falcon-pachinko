"""Optional telemetry cannot take ownership of session state or cancellation."""

from __future__ import annotations

import asyncio
import dataclasses as dc

import pytest

from falcon_pachinko._observability import _observe_lifecycle
from falcon_pachinko.unittests._lifecycle_observer_fixtures import (
    _FailingObserver,
    _observed_router,
    _ObservedResource,
    _RecordingObserver,
)
from falcon_pachinko.unittests.helpers import DummyWS, make_req


def test_cancellation_overrides_a_previously_returned_observation_failure() -> None:
    """A later propagated cancellation supersedes ordinary returned failures."""
    observer = _RecordingObserver()
    cancellation = asyncio.CancelledError("private cancellation detail")

    def fail_then_cancel() -> None:
        """Propagate cancellation through an observation with a returned error."""
        with _observe_lifecycle(
            observer,
            "cleanup",
            span_name="falcon_pachinko.websocket.finalization",
        ) as observation:
            observation.record_failure(ValueError("private cleanup detail"), "cleanup")
            raise cancellation

    with pytest.raises(asyncio.CancelledError) as raised:
        fail_then_cancel()

    assert raised.value is cancellation, "observation must preserve exact cancellation"
    event = observer.events[0]
    assert (event.outcome, event.failure_category) == ("cancelled", "cancelled"), (
        "completion must follow the propagated cancellation"
    )
    assert observer.spans[0].results[0].outcome == "cancelled", (
        "span completion must agree with the metric outcome"
    )
    assert "private" not in repr(observer.events), (
        "failure values must not enter labels"
    )


@pytest.mark.asyncio
async def test_adapter_cancellation_cannot_cancel_a_successful_session() -> None:
    """A failed metrics callback leaves successful dispatch and cleanup intact."""
    observer = _FailingObserver(
        stage="record", error=asyncio.CancelledError("host metrics cancelled")
    )
    resource = _ObservedResource()
    await _observed_router(resource, observer).on_websocket(
        make_req("/session"), DummyWS(("frame",))
    )
    assert resource.frames == ["frame"], "adapter cancellation must not stop dispatch"
    assert resource.disconnects == [1000], "adapter cancellation must not stop cleanup"
    assert all(len(span.results) == 1 for span in observer.spans), (
        "a failed metrics callback must still allow every tracing span to finish"
    )


def test_metric_events_are_immutable_after_observation() -> None:
    """A host adapter cannot mutate an event shared with instrumentation consumers."""
    observer = _RecordingObserver()
    with _observe_lifecycle(observer, "cleanup"):
        pass
    attribute = "outcome"
    with pytest.raises(dc.FrozenInstanceError):
        setattr(observer.events[0], attribute, "failed")
