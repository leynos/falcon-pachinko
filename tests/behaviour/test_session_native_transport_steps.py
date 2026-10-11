"""Focused live session native_transport support."""

from __future__ import annotations

import asyncio
import typing as typ

import pytest
from pytest_bdd import scenario, when

from tests.behaviour._session_context import (
    LiveScenario,
    connect,
    finish_client_session,
)

if typ.TYPE_CHECKING:
    import falcon.asgi

_ASSERTION_FAILURE = "lifecycle expectation failed"


@scenario(
    "session_lifecycle.feature",
    "Non-JSON text reaches the unhandled fallback unchanged",
)
def test_non_json_text() -> None:
    """Register the native transport regression."""


@scenario(
    "session_lifecycle.feature",
    "Native text frames retain their original JSON formatting",
)
def test_native_text_formatting() -> None:
    """Register the native transport regression."""


@scenario(
    "session_lifecycle.feature", "JSON binary frames reach the resource unchanged"
)
def test_json_binary() -> None:
    """Register the native transport regression."""


@scenario(
    "session_lifecycle.feature",
    "Failed close validation leaves the accepted connection usable",
)
def test_failed_close_validation() -> None:
    """Register the native transport regression."""


@scenario(
    "session_lifecycle.feature", "Native Falcon media keyword arguments remain usable"
)
def test_native_media_keywords() -> None:
    """Register the native transport regression."""


@when("the client sends text outside a JSON envelope", target_fixture="context")
def when_client_sends_plain_text(context: LiveScenario) -> LiveScenario:
    """Keep non-JSON text available to the resource fallback."""
    raw = "hello outside an envelope"
    return _exchange_raw_frame(context, raw, {"type": "raw", "message": raw})


@when(
    "the client sends JSON text with its original formatting", target_fixture="context"
)
def when_client_sends_formatted_text(context: LiveScenario) -> LiveScenario:
    """Preserve the original JSON spelling and whitespace."""
    return _exchange_raw_frame(
        context, '{ "type": "echo", "index": 1.00 }', {"type": "echo", "index": 1.0}
    )


@when("the client sends a JSON binary frame", target_fixture="context")
def when_client_sends_binary(context: LiveScenario) -> LiveScenario:
    """Pass JSON binary frames to Pachinko rather than Falcon's msgpack codec."""
    return _exchange_raw_frame(
        context, b'{ "type": "echo", "index": 2 }', {"type": "echo", "index": 2}
    )


def _exchange_raw_frame(
    context: LiveScenario, raw: str | bytes, expected: dict[str, object]
) -> LiveScenario:
    """Verify exact payloads through the native socket and fallback."""

    async def exercise() -> None:
        await connect(context)
        assert context.session is not None, _ASSERTION_FAILURE
        await asyncio.wait_for(context.session.send(raw), timeout=5)
        reply = await asyncio.wait_for(context.session.receive_json(), timeout=5)
        assert reply == expected, "native frames must reach their intended fallback"
        assert context.recorder.received_frames == [raw], (
            "native frames must retain their exact payload and type"
        )
        assert not context.session.closed, "raw fallback must keep the session open"
        await finish_client_session(context)

    context.event_loop.run_until_complete(exercise())
    return context


@when(
    "the manager attempts a reserved close code and then exchanges a message",
    target_fixture="context",
)
def when_manager_close_validation_fails(context: LiveScenario) -> LiveScenario:
    """Verify rejected close arguments leave message handling available."""

    async def exercise() -> None:
        await connect(context)
        conn_id = context.recorder.connection_ids[-1]
        socket = context.server.application.manager.websockets[conn_id]
        with pytest.raises(ValueError, match="Invalid close code"):
            await asyncio.wait_for(socket.close(1006), timeout=5)
        assert not getattr(socket, "closed", False), (
            "failed validation must leave the accepted socket open"
        )
        assert context.session is not None, _ASSERTION_FAILURE
        payload = {"type": "echo", "index": 0}
        await asyncio.wait_for(context.session.send_json(payload), timeout=5)
        reply = await asyncio.wait_for(context.session.receive_json(), timeout=5)
        assert reply == payload, "failed close validation must preserve dispatch"
        await finish_client_session(context)

    context.event_loop.run_until_complete(exercise())
    return context


@when(
    "the manager sends media using Falcon keyword arguments", target_fixture="context"
)
def when_manager_sends_native_media(context: LiveScenario) -> LiveScenario:
    """Preserve native keyword arguments through the acceptance adapter."""

    async def exercise() -> None:
        await connect(context)
        conn_id = context.recorder.connection_ids[-1]
        socket = context.server.application.manager.websockets[conn_id]
        payload = {"type": "native-notice"}
        await asyncio.wait_for(
            typ.cast("falcon.asgi.WebSocket", socket).send_media(media=payload),
            timeout=5,
        )
        assert context.session is not None, _ASSERTION_FAILURE
        reply = await asyncio.wait_for(context.session.receive_json(), timeout=5)
        assert reply == payload, "native media keywords must be forwarded"
        await finish_client_session(context)

    context.event_loop.run_until_complete(exercise())
    return context
