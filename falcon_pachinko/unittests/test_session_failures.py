"""Focused router-owned session failures contracts."""

from __future__ import annotations

import asyncio
import typing as typ

import falcon
import pytest

from falcon_pachinko.hooks import HookEvent
from falcon_pachinko.unittests._session_fixtures import (
    RecordingResource,
    SessionSocket,
    make_session_request,
    make_session_router,
)

_ASSERTION_FAILURE = "lifecycle expectation failed"


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
    assert AcceptThenFail.instances[0].disconnect_codes == [1006], (
        "setup cancellation must still execute the promised cleanup"
    )


@pytest.mark.asyncio
async def test_session_failure_keeps_precedence_over_disconnect_failure() -> None:
    """Disconnect errors stay visible without replacing a handler failure."""
    primary = RuntimeError("handler failed")
    cleanup = LookupError("disconnect failed")

    class FailingResource(RecordingResource):
        async def dispatch(self, ws: object, raw: str | bytes) -> None:
            del ws, raw
            raise primary

        async def on_disconnect(self, ws: object, close_code: int) -> None:
            await super().on_disconnect(ws, close_code)
            raise cleanup

    router = make_session_router(FailingResource)
    socket = SessionSocket(b'{"type":"boom"}')
    loop = asyncio.get_running_loop()
    reported: list[dict[str, object]] = []
    previous_handler = loop.get_exception_handler()
    loop.set_exception_handler(lambda _loop, details: reported.append(details))

    try:
        with pytest.raises(RuntimeError) as error:
            await router.on_websocket(
                typ.cast("typ.Any", make_session_request()), typ.cast("typ.Any", socket)
            )
    finally:
        loop.set_exception_handler(previous_handler)

    assert error.value is primary, _ASSERTION_FAILURE
    assert [details["exception"] for details in reported] == [cleanup], (
        "disconnect failure must be reported after preserving the handler failure"
    )
    assert FailingResource.instances[0].disconnect_codes == [1011], _ASSERTION_FAILURE
    assert socket.close_codes == [1011], _ASSERTION_FAILURE


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
