"""BDD proof of the acceptance contract through real Falcon WebSockets."""

from __future__ import annotations

import asyncio
import typing as typ
from contextlib import suppress

import pytest
from pytest_bdd import scenario, then, when
from websockets.exceptions import ConnectionClosed, InvalidStatusCode

from tests.behaviour._session_context import LiveScenario, connect

_ASSERTION_FAILURE = "live lifecycle expectation failed"


def _open_and_close_session(
    context: LiveScenario,
    path: str = "/ws/session",
    headers: dict[str, str] | None = None,
) -> LiveScenario:
    """Complete a successful live connection and wait for disconnect cleanup."""

    async def exercise() -> None:
        await connect(context, path, headers=headers)
        await asyncio.wait_for(context.recorder.connection_ready.wait(), timeout=5)
        assert context.session is not None, _ASSERTION_FAILURE
        await asyncio.wait_for(context.session.close(1000), timeout=5)
        await context.recorder.wait_for_disconnect(context.recorder.connection_ids[-1])

    context.event_loop.run_until_complete(exercise())
    return context


@scenario(
    "live_lifecycle.feature",
    "Missing request metadata rejects before acceptance",
)
def test_missing_metadata_rejection() -> None:  # pragma: no cover - BDD registration
    """Register live pre-accept rejection coverage."""


@scenario(
    "live_lifecycle.feature",
    "Request metadata can approve acceptance",
)
def test_request_metadata_approval() -> None:  # pragma: no cover
    """Register live request-metadata approval coverage."""


@scenario(
    "live_lifecycle.feature",
    "Successful negotiation accepts exactly once",
)
def test_successful_acceptance_once() -> None:  # pragma: no cover - BDD registration
    """Register successful single-acceptance coverage."""


@scenario(
    "live_lifecycle.feature",
    "Deprecated resource acceptance remains single-call compatible",
)
def test_deprecated_acceptance_once() -> None:  # pragma: no cover - BDD registration
    """Register compatibility-path single-acceptance coverage."""


@scenario(
    "live_lifecycle.feature",
    "Post-accept setup exchanges a hello frame",
)
def test_post_accept_hello_exchange() -> None:  # pragma: no cover - BDD registration
    """Register active-session send and receive coverage."""


@scenario(
    "live_lifecycle.feature",
    "Native media receive retains Falcon decoding",
)
def test_native_media_decoding() -> None:  # pragma: no cover
    """Register public native-media receive coverage through the live socket."""


@scenario(
    "live_lifecycle.feature",
    "Session-start failure closes and removes registration",
)
def test_session_start_failure_cleanup() -> None:  # pragma: no cover
    """Register post-accept failure and cleanup coverage."""


@scenario(
    "live_lifecycle.feature",
    "Nested hooks follow the documented lifecycle order",
)
def test_nested_lifecycle_hook_order() -> None:  # pragma: no cover
    """Register live nested hook-order coverage."""


@when("the client attempts a missing-header rejection", target_fixture="context")
def when_missing_header(context: LiveScenario) -> LiveScenario:
    """Attempt an upgrade with no token metadata and retain the HTTP response."""

    async def exercise() -> None:
        try:
            await connect(context, "/ws/reject")
        except InvalidStatusCode as exc:
            context.rejected_handshake = exc
        else:
            pytest.fail("missing request metadata must reject the upgrade")

    context.event_loop.run_until_complete(exercise())
    return context


@when("the client provides the required request metadata", target_fixture="context")
def when_request_metadata_approves(context: LiveScenario) -> LiveScenario:
    """Accept only when the real request carries its required token header."""
    return _open_and_close_session(
        context,
        "/ws/reject",
        headers={"x-session-token": "live-token"},
    )


@then("the upgrade is rejected without an accepted session")
def then_rejected_before_accept(context: LiveScenario) -> None:
    """Prove the real Falcon socket never accepted or started a session."""
    assert context.rejected_handshake is not None, _ASSERTION_FAILURE
    assert (
        typ.cast("InvalidStatusCode", context.rejected_handshake).status_code == 403
    ), "pre-accept rejection must return HTTP 403"
    assert context.recorder.accept_calls == 0, _ASSERTION_FAILURE
    assert context.recorder.connection_ids == [], _ASSERTION_FAILURE
    assert context.recorder.disconnect_records == [], _ASSERTION_FAILURE
    assert context.recorder.rejected_disconnects == 0, _ASSERTION_FAILURE


@when("the client opens and closes an accepted session", target_fixture="context")
def when_successful_session(context: LiveScenario) -> LiveScenario:
    """Open a live accepted connection and finish its normal lifecycle."""
    return _open_and_close_session(context)


@when("the client opens a deprecated-accept session", target_fixture="context")
def when_legacy_accept(context: LiveScenario) -> LiveScenario:
    """Exercise a resource that still calls deprecated ws.accept()."""
    return _open_and_close_session(context, "/ws/deprecated-accept")


@then("the live transport records one acceptance")
def then_exactly_one_accept(context: LiveScenario) -> None:
    """Assert that the delegate observed one real Falcon accept call."""
    assert context.recorder.accept_calls == 1, _ASSERTION_FAILURE
    assert len(context.recorder.disconnect_records) == 1, _ASSERTION_FAILURE


@when("the client completes the post-accept hello exchange", target_fixture="context")
def when_hello_exchange(context: LiveScenario) -> LiveScenario:
    """Send and receive frames from on_session_start over a live socket."""
    frames: list[str] = []

    async def exercise() -> None:
        await connect(context, "/ws/hello")
        assert context.session is not None, _ASSERTION_FAILURE
        frames.append(await asyncio.wait_for(context.session.receive_text(), timeout=5))
        await asyncio.wait_for(context.session.send_text("client hello"), timeout=5)
        frames.append(await asyncio.wait_for(context.session.receive_text(), timeout=5))
        await asyncio.wait_for(context.session.close(1000), timeout=5)
        await context.recorder.wait_for_disconnect(context.recorder.connection_ids[-1])

    context.event_loop.run_until_complete(exercise())
    context.post_accept_frames = frames
    return context


@then("both session-start frames arrive over the live connection")
def then_hello_frames(context: LiveScenario) -> None:
    """Check the hello and deadline-bound reply both crossed the upgrade."""
    assert context.post_accept_frames == [
        "server hello",
        "confirmed: client hello",
    ], _ASSERTION_FAILURE
    assert context.session is not None, _ASSERTION_FAILURE
    assert context.session.close_code == 1000, _ASSERTION_FAILURE


@when("the client sends JSON during post-accept setup", target_fixture="context")
def when_client_sends_json_during_setup(context: LiveScenario) -> LiveScenario:
    """Use the public media receiver during setup and retain decoded objects."""
    payload = {"token": "session-secret"}

    async def exercise() -> None:
        await connect(context, "/ws/decoded-media")
        assert context.session is not None, _ASSERTION_FAILURE
        await asyncio.wait_for(context.session.send_json(payload), timeout=5)
        reply = await asyncio.wait_for(context.session.receive_json(), timeout=5)
        assert reply == {"received": payload}, _ASSERTION_FAILURE
        await asyncio.wait_for(context.session.close(1000), timeout=5)
        await context.recorder.wait_for_disconnect(context.recorder.connection_ids[-1])

    context.event_loop.run_until_complete(exercise())
    return context


@then("the application receives decoded JSON media")
def then_media_is_decoded(context: LiveScenario) -> None:
    """Ensure the guarded public receive_media API keeps Falcon decoding."""
    assert context.recorder.decoded_media == [{"token": "session-secret"}], (
        _ASSERTION_FAILURE
    )


@when("post-accept setup fails after manager registration", target_fixture="context")
def when_session_start_fails(context: LiveScenario) -> LiveScenario:
    """Observe the live close and wait until disconnect cleanup completes."""

    async def exercise() -> None:
        await connect(context, "/ws/start-failure")
        assert context.session is not None, _ASSERTION_FAILURE
        with suppress(ConnectionClosed):
            await asyncio.wait_for(context.session.receive(), timeout=5)
        await asyncio.wait_for(context.recorder.connection_ready.wait(), timeout=5)
        await context.recorder.wait_for_disconnect(context.recorder.connection_ids[-1])

    context.event_loop.run_until_complete(exercise())
    return context


@then("the live session closes with 1011 and finalizes once")
def then_start_failure_finalizes(context: LiveScenario) -> None:
    """Check close code, propagated error context, and manager cleanup."""
    assert context.session is not None, _ASSERTION_FAILURE
    assert context.session.close_code == 1011, _ASSERTION_FAILURE
    assert len(context.recorder.disconnect_records) == 1, _ASSERTION_FAILURE
    assert context.recorder.disconnect_records[0][1] == 1011, _ASSERTION_FAILURE
    assert len(context.recorder.before_disconnect_errors) == 1, _ASSERTION_FAILURE
    error = context.recorder.before_disconnect_errors[0]
    assert isinstance(error, RuntimeError), _ASSERTION_FAILURE
    assert context.server.application.manager.websockets == {}, _ASSERTION_FAILURE


@when(
    "the client exchanges one frame on a nested route and closes normally",
    target_fixture="context",
)
def when_nested_session(context: LiveScenario) -> LiveScenario:
    """Capture connect, acceptance, session, receive, and disconnect ordering."""

    async def exercise() -> None:
        await connect(context, "/ws/lifecycle/child")
        assert context.session is not None, _ASSERTION_FAILURE
        await asyncio.wait_for(context.recorder.connection_ready.wait(), timeout=5)
        await asyncio.wait_for(context.session.send_json({"type": "echo"}), timeout=5)
        await asyncio.wait_for(context.session.receive_json(), timeout=5)
        await asyncio.wait_for(context.recorder.message_processed.wait(), timeout=5)
        await asyncio.wait_for(context.session.close(1000), timeout=5)
        await context.recorder.wait_for_disconnect(context.recorder.connection_ids[-1])

    context.event_loop.run_until_complete(exercise())
    return context


@then("the live hook events match the documented order")
def then_lifecycle_order(context: LiveScenario) -> None:
    """Compare the recorder with the public nested lifecycle sequence."""
    assert context.recorder.events == [
        "global.before_connect",
        "parent.before_connect",
        "child.before_connect",
        "child.on_connect",
        "child.after_connect",
        "parent.after_connect",
        "global.after_connect",
        "accept",
        "child.on_session_start",
        "global.before_receive",
        "parent.before_receive",
        "child.before_receive",
        "child.dispatch",
        "child.after_receive",
        "parent.after_receive",
        "global.after_receive",
        "global.before_disconnect",
        "parent.before_disconnect",
        "child.before_disconnect",
        "child.on_disconnect",
    ], _ASSERTION_FAILURE
    assert context.recorder.before_disconnect_errors == [None], _ASSERTION_FAILURE
