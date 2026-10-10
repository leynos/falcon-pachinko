"""Live ASGI behaviour tests for router-owned WebSocket sessions."""

from __future__ import annotations

import asyncio
import dataclasses as dc
import typing as typ

import msgspec as ms
import msgspec.json as msjson
import pytest
from pytest_bdd import given, scenario, then, when
from websockets.exceptions import ConnectionClosed

from falcon_pachinko import WebSocketLike, WebSocketResource, handles_message

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from ._live_server import LiveServerContext, SessionRecorder

pytest_plugins = ("tests.behaviour._live_server",)


class EchoMessage(ms.Struct, tag="echo"):
    """A message acknowledged by the live session resource."""

    payload: str


class FailMessage(ms.Struct, tag="fail"):
    """A message whose handler raises to exercise the failure policy."""

    payload: str


SessionMessage = EchoMessage | FailMessage


class LiveSessionResource(WebSocketResource):
    """Record live dispatch and send replies through Falcon's socket."""

    schema = SessionMessage

    def __init__(self, recorder: SessionRecorder) -> None:
        self.recorder = recorder

    @handles_message("echo")
    async def echo(self, ws: WebSocketLike, message: EchoMessage) -> None:
        """Record and reply to one decoded echo message."""
        self.recorder.handler_calls.append(message.payload)
        await ws.send_media({"type": "reply", "payload": message.payload})

    @handles_message("fail")
    async def fail(self, ws: WebSocketLike, message: FailMessage) -> None:
        """Record and raise an unexpected handler failure."""
        del ws, message
        failure = RuntimeError("recorded live handler failure")
        self.recorder.handler_failure = failure
        raise failure

    async def on_unhandled(self, ws: WebSocketLike, message: str | bytes) -> None:
        """Record an unknown frame and send a fallback response."""
        self.recorder.unhandled_messages.append(message)
        await ws.send_media({"type": "error", "payload": "unsupported message"})

    async def on_disconnect(self, ws: WebSocketLike, close_code: int) -> None:
        """Record the session close code for lifecycle assertions."""
        del ws
        self.recorder.disconnect_codes.append(close_code)
        self.recorder.disconnected.set()


@dc.dataclass(slots=True)
class RouterSessionScenario:
    """State shared by the live-server BDD steps."""

    context: LiveServerContext
    replies: list[object] = dc.field(default_factory=list)
    close_observation: int | None = None


def _resource_factory(recorder: SessionRecorder) -> WebSocketResource:
    return LiveSessionResource(recorder)


@scenario("router_session.feature", "A tagged message receives a server reply")
def test_router_session_reply() -> None:  # pragma: no cover - BDD registration
    """Register the live reply scenario."""


@scenario(
    "router_session.feature",
    "Multiple frames are dispatched in order on one connection",
)
def test_router_session_ordered_frames() -> None:  # pragma: no cover - BDD registration
    """Register the ordered multi-frame scenario."""


@scenario(
    "router_session.feature",
    "An unknown message leaves the connection usable",
)
def test_router_session_unknown_message() -> (
    None
):  # pragma: no cover - BDD registration
    """Register the unknown-message fallback scenario."""


@scenario(
    "router_session.feature",
    "A handler failure closes the connection and is reported",
)
def test_router_session_handler_failure() -> (
    None
):  # pragma: no cover - BDD registration
    """Register the failure policy scenario."""


@scenario(
    "router_session.feature",
    "A client close reaches the disconnect lifecycle",
)
def test_router_session_client_close() -> None:  # pragma: no cover - BDD registration
    """Register the client-close lifecycle scenario."""


@given("a live Falcon router session", target_fixture="context")
def given_live_session(live_server: cabc.Callable) -> RouterSessionScenario:
    """Start Falcon ASGI for a real client WebSocket connection."""
    context = live_server(_resource_factory)
    return RouterSessionScenario(context)


@when("the client sends an echo message", target_fixture="context")
def when_send_echo(context: RouterSessionScenario) -> RouterSessionScenario:
    """Send one tagged message and receive its reply on the live socket."""

    async def exchange() -> None:
        async with context.context.client.connect("/ws") as session:
            await session.send_json(EchoMessage(payload="hello"))
            context.replies.append(await asyncio.wait_for(session.receive_json(), 5))

    context.context.event_loop.run_until_complete(exchange())
    return context


@when(
    "the client sends three ordered messages including a binary frame",
    target_fixture="context",
)
def when_send_ordered(context: RouterSessionScenario) -> RouterSessionScenario:
    """Send text, binary, and text frames and receive each reply in order."""

    async def exchange() -> None:
        async with context.context.client.connect("/ws") as session:
            await session.send_json(EchoMessage(payload="first"))
            await session.send_bytes(msjson.encode(EchoMessage(payload="second")))
            await session.send_json(EchoMessage(payload="third"))
            for _ in range(3):
                context.replies.append(
                    await asyncio.wait_for(session.receive_json(), 5)
                )

    context.context.event_loop.run_until_complete(exchange())
    return context


@when(
    "the client sends an unknown message followed by a known message",
    target_fixture="context",
)
def when_send_unknown_then_known(
    context: RouterSessionScenario,
) -> RouterSessionScenario:
    """Verify fallback and ordinary dispatch share one open session."""

    async def exchange() -> None:
        async with context.context.client.connect("/ws") as session:
            await session.send_json({"type": "unknown", "payload": "skip"})
            context.replies.append(await asyncio.wait_for(session.receive_json(), 5))
            await session.send_json(EchoMessage(payload="still open"))
            context.replies.append(await asyncio.wait_for(session.receive_json(), 5))

    context.context.event_loop.run_until_complete(exchange())
    return context


@when(
    "the client sends a message whose handler fails",
    target_fixture="context",
)
def when_send_failure(context: RouterSessionScenario) -> RouterSessionScenario:
    """Observe the server failure as a WebSocket close frame."""

    async def exchange() -> None:
        async with context.context.client.connect("/ws") as session:
            await session.send_json(FailMessage(payload="raise"))
            with pytest.raises(ConnectionClosed):
                await asyncio.wait_for(session.receive_text(), 5)
            await asyncio.wait_for(context.context.recorder.disconnected.wait(), 5)
            context.close_observation = session.close_code

    context.context.event_loop.run_until_complete(exchange())
    return context


@when("the client closes with code 4008", target_fixture="context")
def when_client_closes(context: RouterSessionScenario) -> RouterSessionScenario:
    """Close the live connection and wait for server lifecycle completion."""

    async def exchange() -> None:
        async with context.context.client.connect("/ws") as session:
            await session.close(code=4008, reason="client test close")
            await asyncio.wait_for(context.context.recorder.disconnected.wait(), 5)
            context.close_observation = session.close_code

    context.context.event_loop.run_until_complete(exchange())
    return context


@then("the client receives the echo reply")
def then_echo_reply(context: RouterSessionScenario) -> None:
    """Assert the handler response reached the client."""
    assert context.replies == [{"type": "reply", "payload": "hello"}], (
        "the client should receive the handler's echo response"
    )


@then("the router records the dispatched message")
def then_records_echo(context: RouterSessionScenario) -> None:
    """Assert the resource handler ran with the message payload."""
    assert context.context.recorder.handler_calls == ["hello"], (
        "the router should dispatch the received payload to its handler"
    )


@then("the server replies in the same order")
def then_ordered_replies(context: RouterSessionScenario) -> None:
    """Assert all replies preserve the client's frame order."""
    assert context.replies == [
        {"type": "reply", "payload": value} for value in ("first", "second", "third")
    ], "the client should receive replies in the order messages were sent"


@then("the router records all three messages in order")
def then_ordered_dispatch(context: RouterSessionScenario) -> None:
    """Assert handler calls preserve the live connection's frame order."""
    assert context.context.recorder.handler_calls == ["first", "second", "third"], (
        "the router should dispatch all three messages in frame order"
    )


@then("the unknown message receives the fallback reply")
def then_unknown_fallback(context: RouterSessionScenario) -> None:
    """Assert the unknown tag reaches on_unhandled and gets its response."""
    assert context.context.recorder.unhandled_messages == [
        '{"type":"unknown","payload":"skip"}'
    ], "the unknown tagged message should reach on_unhandled"
    assert context.replies[0] == {
        "type": "error",
        "payload": "unsupported message",
    }, "on_unhandled should reply with its documented fallback payload"


@then("the known message is dispatched on the same connection")
def then_known_after_unknown(context: RouterSessionScenario) -> None:
    """Assert a valid message still dispatches after the fallback."""
    assert context.replies[1] == {"type": "reply", "payload": "still open"}, (
        "the open connection should receive the later valid message's reply"
    )
    assert context.context.recorder.handler_calls == ["still open"], (
        "the router should keep dispatching after an unknown message"
    )


@then("the client observes close code 1011")
def then_failure_close_code(context: RouterSessionScenario) -> None:
    """Assert unexpected handler failure closes with the server-error code."""
    assert context.close_observation == 1011, (
        "an unexpected handler failure should close the live socket with 1011"
    )


@then("the router records the original handler failure and disconnect code")
def then_failure_recorded(context: RouterSessionScenario) -> None:
    """Assert the original exception and 1011 lifecycle code were retained."""
    recorder = context.context.recorder
    assert isinstance(recorder.handler_failure, RuntimeError), (
        "the recorder should retain the original handler exception"
    )
    assert str(recorder.handler_failure) == "recorded live handler failure", (
        "the original handler exception should keep its message"
    )
    assert recorder.after_receive_errors == [recorder.handler_failure], (
        "the after-receive hook should observe the original handler exception"
    )
    assert recorder.disconnect_codes == [1011], (
        "the disconnect lifecycle should receive the server-error close code"
    )


@then("the resource receives the client close code")
def then_client_close_code(context: RouterSessionScenario) -> None:
    """Assert the live resource sees the peer's close code."""
    assert context.context.recorder.disconnect_codes == [4008], (
        "the disconnect lifecycle should receive the client's close code"
    )
