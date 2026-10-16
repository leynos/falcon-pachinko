"""Focused router-owned session failures contracts."""

from __future__ import annotations

import asyncio
import logging
import typing as typ

import falcon
import pytest
from hypothesis import given
from hypothesis import strategies as st

from falcon_pachinko._session_cleanup import raise_session_failures
from falcon_pachinko.hooks import HookEvent
from falcon_pachinko.unittests._session_fixtures import (
    RecordingResource,
    SessionSocket,
    make_session_request,
    make_session_router,
)

_ASSERTION_FAILURE = "lifecycle expectation failed"
_ORDINARY_FAILURE_TYPES = (ValueError, RuntimeError, LookupError)


@given(
    primary_type=st.one_of(st.none(), st.sampled_from(_ORDINARY_FAILURE_TYPES)),
    cleanup_types=st.lists(st.sampled_from(_ORDINARY_FAILURE_TYPES), max_size=3),
)
def test_generated_ordinary_failures_preserve_primary_first_order(
    primary_type: type[Exception] | None,
    cleanup_types: list[type[Exception]],
) -> None:
    """Generated ordinary failures keep identity and primary-first precedence."""
    primary = primary_type("primary") if primary_type is not None else None
    cleanup_failures = tuple(
        failure_type(f"cleanup-{index}")
        for index, failure_type in enumerate(cleanup_types)
    )
    expected = ((primary,) if primary is not None else ()) + cleanup_failures

    if not expected:
        raise_session_failures(None, (), ())
        return

    if len(expected) == 1:
        with pytest.raises(type(expected[0])) as raised:
            raise_session_failures(primary, cleanup_failures, ())
        assert raised.value is expected[0], _ASSERTION_FAILURE
    else:
        with pytest.raises(ExceptionGroup) as raised:
            raise_session_failures(primary, cleanup_failures, ())
        assert raised.value.exceptions == expected, _ASSERTION_FAILURE


@pytest.mark.asyncio
async def test_accepted_on_connect_failure_and_after_hook_failure_are_grouped() -> None:
    """An after-connect error cannot hide the original accepted setup failure."""
    primary = ValueError("setup failed")
    hook_error = RuntimeError("after-connect failed")

    class AcceptThenFail(RecordingResource):
        async def on_connect(
            self, req: falcon.Request, ws: object, **params: object
        ) -> bool:
            del req, params
            await typ.cast("SessionSocket", ws).accept()
            raise primary

    def fail_after_connect(context: object) -> None:
        del context
        raise hook_error

    router = make_session_router(AcceptThenFail)
    router.global_hooks.add(HookEvent.AFTER_CONNECT, fail_after_connect)
    socket = SessionSocket()

    with pytest.raises(ExceptionGroup, match="session") as raised:
        await router.on_websocket(
            typ.cast("typ.Any", make_session_request()), typ.cast("typ.Any", socket)
        )

    assert raised.value.exceptions == (primary, hook_error), (
        "setup failures must remain visible before the after-connect failure"
    )
    assert AcceptThenFail.instances[0].disconnect_codes == [1011], (
        "failed setup must still execute the promised cleanup"
    )


@pytest.mark.asyncio
async def test_after_connect_cancellation_reports_original_setup_failure() -> None:
    """Cancellation stays bare while the original setup failure is reported."""
    primary = ValueError("setup failed")
    cancellation = asyncio.CancelledError("after-connect cancelled")

    class AcceptThenFail(RecordingResource):
        async def on_connect(
            self, req: falcon.Request, ws: object, **params: object
        ) -> bool:
            del req, params
            await typ.cast("SessionSocket", ws).accept()
            raise primary

    def cancel_after_connect(context: object) -> None:
        del context
        raise cancellation

    router = make_session_router(AcceptThenFail)
    router.global_hooks.add(HookEvent.AFTER_CONNECT, cancel_after_connect)
    socket = SessionSocket()
    loop = asyncio.get_running_loop()
    reported: list[object] = []
    previous_handler = loop.get_exception_handler()
    loop.set_exception_handler(
        lambda _loop, context: reported.append(context.get("exception"))
    )
    try:
        with pytest.raises(asyncio.CancelledError) as raised:
            await router.on_websocket(
                typ.cast("typ.Any", make_session_request()), typ.cast("typ.Any", socket)
            )
    finally:
        loop.set_exception_handler(previous_handler)

    assert raised.value is cancellation, "the original cancellation must stay bare"
    assert reported == [primary], "the original setup failure must stay observable"
    assert AcceptThenFail.instances[0].disconnect_codes == [1001], (
        "setup cancellation must still execute the promised cleanup"
    )


@pytest.mark.asyncio
async def test_session_and_disconnect_failures_are_grouped_primary_first(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Disconnect failures are visible after the original handler failure."""
    caplog.set_level(logging.DEBUG, logger="falcon_pachinko._session_driver")
    primary = RuntimeError("CANARY-handler-secret")
    cleanup = LookupError("CANARY-cleanup-secret")

    class FailingResource(RecordingResource):
        async def dispatch(self, ws: object, raw: str | bytes) -> None:
            del ws, raw
            raise primary

        async def on_disconnect(self, ws: object, close_code: int) -> None:
            await super().on_disconnect(ws, close_code)
            raise cleanup

    router = make_session_router(FailingResource)
    socket = SessionSocket(b"CANARY-frame-secret")

    with pytest.raises(ExceptionGroup) as error:
        await router.on_websocket(
            typ.cast("typ.Any", make_session_request()), typ.cast("typ.Any", socket)
        )

    assert error.value.exceptions == (primary, cleanup), _ASSERTION_FAILURE
    assert FailingResource.instances[0].disconnect_codes == [1011], _ASSERTION_FAILURE
    assert socket.close_codes == [1011], _ASSERTION_FAILURE
    observations = [
        record
        for record in caplog.records
        if record.name == "falcon_pachinko._session_driver"
    ]
    cleanup_observations = [
        record
        for record in observations
        if getattr(record, "operation", None) == "cleanup"
    ]
    cleanup_observation = cleanup_observations[-1]
    session_observation = observations[-1]
    assert getattr(cleanup_observation, "outcome", None) == "failed", _ASSERTION_FAILURE
    assert getattr(cleanup_observation, "close_code", None) == 1011, _ASSERTION_FAILURE
    assert getattr(cleanup_observation, "cleanup_failure_types", None) == (
        "LookupError",
    ), _ASSERTION_FAILURE
    assert getattr(session_observation, "outcome", None) == "failed", _ASSERTION_FAILURE
    assert getattr(session_observation, "exception_type", None) == "ExceptionGroup", (
        _ASSERTION_FAILURE
    )
    assert "CANARY" not in caplog.text, "observations must omit exception values"


@pytest.mark.asyncio
async def test_cleanup_only_failure_is_propagated_unchanged() -> None:
    """A lone resource cleanup failure remains the original exception."""
    cleanup = LookupError("disconnect failed")

    class FailingCleanupResource(RecordingResource):
        async def on_disconnect(self, ws: object, close_code: int) -> None:
            await super().on_disconnect(ws, close_code)
            raise cleanup

    router = make_session_router(FailingCleanupResource)
    socket = SessionSocket(falcon.WebSocketDisconnected(4000))

    with pytest.raises(LookupError) as error:
        await router.on_websocket(
            typ.cast("typ.Any", make_session_request()), typ.cast("typ.Any", socket)
        )

    assert error.value is cleanup, _ASSERTION_FAILURE


@pytest.mark.asyncio
async def test_before_disconnect_failure_does_not_skip_resource_cleanup() -> None:
    """The resource hook runs even when a before-disconnect hook fails."""
    before_error = RuntimeError("before disconnect failed")
    resource_error = ValueError("resource cleanup failed")

    class FailingCleanupResource(RecordingResource):
        async def on_disconnect(self, ws: object, close_code: int) -> None:
            await super().on_disconnect(ws, close_code)
            raise resource_error

    router = make_session_router(FailingCleanupResource)

    def fail_before_disconnect(context: object) -> None:
        del context
        raise before_error

    router.global_hooks.add(HookEvent.BEFORE_DISCONNECT, fail_before_disconnect)
    socket = SessionSocket(falcon.WebSocketDisconnected(4000))

    with pytest.raises(ExceptionGroup) as error:
        await router.on_websocket(
            typ.cast("typ.Any", make_session_request()), typ.cast("typ.Any", socket)
        )

    assert error.value.exceptions == (before_error, resource_error), _ASSERTION_FAILURE
    assert FailingCleanupResource.instances[0].disconnect_codes == [4000], (
        _ASSERTION_FAILURE
    )


@pytest.mark.asyncio
async def test_before_disconnect_receives_code_and_primary_error() -> None:
    """Disconnect context exposes the effective code and original failure."""
    primary = RuntimeError("dispatch failed")
    context_values: list[tuple[int | None, BaseException | None]] = []

    def record_before_disconnect(context: object) -> None:
        typed_context = typ.cast("typ.Any", context)
        context_values.append((typed_context.close_code, typed_context.error))

    class FailingDispatchResource(RecordingResource):
        async def dispatch(self, ws: object, raw: str | bytes) -> None:
            del ws, raw
            raise primary

    router = make_session_router(FailingDispatchResource)
    router.global_hooks.add(HookEvent.BEFORE_DISCONNECT, record_before_disconnect)
    socket = SessionSocket(b'{"type":"failed"}')

    with pytest.raises(RuntimeError) as await_error:
        await router.on_websocket(
            typ.cast("typ.Any", make_session_request()), typ.cast("typ.Any", socket)
        )

    assert await_error.value is primary, _ASSERTION_FAILURE
    assert context_values == [(1011, primary)], _ASSERTION_FAILURE
