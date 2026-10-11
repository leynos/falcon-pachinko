"""Prove safe diagnostics across accepted authentication-message dispatch.

Canaries start inside short frames that truncation would expose. These tests
also preserve trusted hook, fallback, and trace access to the original frames.
Run with ``uv run pytest tests/test_diagnostics_lifecycle_regression.py``.
"""

from __future__ import annotations

import json
import logging
import traceback
import typing as typ
from types import SimpleNamespace
from unittest import mock

import msgspec as ms
import pytest

from falcon_pachinko import WebSocketResource, handles_message
from falcon_pachinko.hooks import HookCollection, HookContext, HookManager
from falcon_pachinko.testing.client import WebSocketSession
from tests._stubs import RecordingWebSocket

if typ.TYPE_CHECKING:
    from websockets.client import WebSocketClientProtocol

    from falcon_pachinko.testing import TraceEvent

CANARY = "CANARY-lifecycle-auth-61"


class AuthHello(ms.Struct, tag="client.hello", forbid_unknown_fields=True):
    """Require structural validation independently of authentication policy."""

    token: str
    count: int = 1


class AuthEnvelopeResource(WebSocketResource):
    """Retain fallback frames without placing them in an application error."""

    def __init__(self) -> None:
        self.fallback_raw: str | bytes | None = None

    @handles_message("client.hello")
    async def authenticate(self, ws: object, payload: AuthHello) -> None:
        """Reject accidental invocation with an invalid authentication frame."""
        msg = "invalid authentication frame reached handler"
        raise AssertionError(msg)

    async def on_unhandled(self, ws: object, message: str | bytes) -> None:
        """Preserve trusted fallback access and expose any retained error chain."""
        self.fallback_raw = message
        msg = "authentication frame rejected"
        raise RuntimeError(msg)


class AuthSchemaResource(AuthEnvelopeResource):
    """Use typed schema dispatch with the same application handler."""

    schema = AuthHello


def _authentication_frame(
    resource_type: type[AuthEnvelopeResource], variant: str
) -> str:
    """Place a token near the beginning of each short authentication frame."""
    payload: dict[str, object] = {"token": CANARY, "count": 1}
    tag = "client.hello"
    match variant:
        case "invalid-type":
            payload["count"] = CANARY
        case "unknown-tag":
            tag = "client.unknown"
        case "strict-extra":
            payload["password"] = CANARY
    frame = (
        {**payload, "type": tag}
        if resource_type is AuthSchemaResource
        else {"payload": payload, "type": tag}
    )
    text = json.dumps(frame, separators=(",", ":"))
    return text[:-1] if variant == "malformed" else text


@pytest.mark.parametrize("resource_type", [AuthEnvelopeResource, AuthSchemaResource])
@pytest.mark.parametrize(
    "variant", ["malformed", "invalid-type", "unknown-tag", "strict-extra"]
)
@pytest.mark.parametrize("binary", [False, True])
@pytest.mark.asyncio
async def test_post_accept_authentication_diagnostics_omit_canaries(
    resource_type: type[AuthEnvelopeResource],
    variant: str,
    caplog: pytest.LogCaptureFixture,
    *,
    binary: bool,
) -> None:
    """All framework displays omit canaries while trusted values remain original."""
    text = _authentication_frame(resource_type, variant)
    assert len(text) < 200, (
        "short frames must demonstrate that truncation is ineffective"
    )
    assert text.index(CANARY) < 40, "the canary must occur near the start of the frame"
    raw = text.encode() if binary else text
    resource = resource_type()
    ws = RecordingWebSocket()
    await ws.accept()
    contexts: list[HookContext] = []
    global_hooks = HookCollection()
    global_hooks.add("before_receive", contexts.append)
    global_hooks.add("after_receive", contexts.append)
    resource.bind_hook_manager(
        HookManager(global_hooks=global_hooks, resources=(resource,))
    )
    trace: list[TraceEvent] = []
    session = WebSocketSession(
        typ.cast(
            "WebSocketClientProtocol",
            SimpleNamespace(recv=mock.AsyncMock(return_value=raw)),
        ),
        path="/auth",
        trace=trace,
    )
    with caplog.at_level(logging.DEBUG):
        received = await session.receive()
        assert isinstance(received, str | bytes), (
            "the trace client must preserve frame types"
        )
        with pytest.raises(RuntimeError) as caught:
            await resource.dispatch(ws, received)
    diagnostics = "\n".join([
        str(caught.value),
        repr(caught.value),
        "".join(traceback.format_exception(caught.value)),
        caplog.text,
        repr(contexts),
        repr(trace),
        repr([event.summary() for event in trace]),
    ])
    assert CANARY not in diagnostics, (
        "default lifecycle diagnostics must omit the canary"
    )
    assert ws.accepted, "authentication frames must be exercised after acceptance"
    assert caught.value.__context__ is None, (
        "fallback failures must detach vendor context"
    )
    assert len(contexts) == 2, "both registered receive hooks must run"
    assert all(context.raw is raw for context in contexts), (
        "hooks must retain original raw frames"
    )
    assert resource.fallback_raw is raw, "fallback must receive the original raw frame"
    assert trace[0].payload is raw, "trace storage must retain the original frame"
