"""Bounded model checks for guarded WebSocket lifecycle operation sequences."""

from __future__ import annotations

import asyncio
import dataclasses as dc
import typing as typ

import pytest
from hypothesis import example, given, settings
from hypothesis import strategies as st

from falcon_pachinko._disconnect import _PeerDisconnectedError
from falcon_pachinko._session import SessionPhase, SessionState
from falcon_pachinko.lifecycle import ConnectionPhase, LifecycleError, LifecycleGuard

type _Operation = typ.Literal[
    "accept", "send", "receive", "peer_disconnect", "close", "cancel", "cleanup"
]
_OPERATIONS: tuple[_Operation, ...] = (
    "accept",
    "send",
    "receive",
    "peer_disconnect",
    "close",
    "cancel",
    "cleanup",
)


class _ModelTransport:
    """Controllable transport that records lifecycle-sensitive operations."""

    def __init__(self) -> None:
        """Expose transport calls and controlled suspension points to the model."""
        self.unaccepted = True
        self.closed = False
        self.close_code: int | None = None
        self.accept_calls = 0
        self.successful_accepts = 0
        self.send_calls = 0
        self.receive_calls = 0
        self.data_calls_before_acceptance = 0
        self.accept_started = asyncio.Event()
        self.release_accept = asyncio.Event()
        self.release_accept.set()
        self.pause_accept = False
        self.receive_started = asyncio.Event()
        self.release_receive = asyncio.Event()
        self.release_receive.set()
        self.pause_receive = False
        self.disconnect_next = False

    async def accept(
        self, subprotocol: str | None = None, headers: object = None
    ) -> None:
        """Record a handshake and optionally suspend before accepting."""
        del subprotocol, headers
        self.accept_calls += 1
        if self.pause_accept:
            self.accept_started.set()
            await self.release_accept.wait()
        self.unaccepted = False
        self.successful_accepts += 1

    async def close(self, code: int | None = 1000, reason: str | None = None) -> None:
        """Keep the first modeled close transition and its effective code."""
        del reason
        if self.closed:
            return
        self.closed = True
        self.close_code = 1000 if code is None else code

    async def send_media(self, data: object) -> None:
        """Count data calls that escape the guard outside its accepted phase."""
        del data
        self.send_calls += 1
        if self.unaccepted or self.closed:
            self.data_calls_before_acceptance += 1

    async def send_text(self, message: str) -> None:
        """Route text through the same modeled data-operation counter."""
        await self.send_media(message)

    async def receive_media(self) -> object:
        """Yield a frame, peer disconnect, or controlled receive suspension."""
        self.receive_calls += 1
        if self.unaccepted or self.closed:
            self.data_calls_before_acceptance += 1
        if self.pause_receive:
            self.receive_started.set()
            await self.release_receive.wait()
        if self.disconnect_next:
            self.closed = True
            self.close_code = 4001
            raise _PeerDisconnectedError(code=4001)
        return "frame"

    async def receive_text(self) -> str:
        """Check the model's text boundary after applying its receive schedule."""
        message = await self.receive_media()
        if not isinstance(message, str):
            msg = "model transport returned a non-text frame"
            raise TypeError(msg)
        return message


async def _cancel_pending_accept(
    guard: LifecycleGuard, transport: _ModelTransport
) -> None:
    """Cancel acceptance while the transport is suspended before accepting."""
    transport.pause_accept = True
    transport.accept_started.clear()
    transport.release_accept.clear()
    task = asyncio.create_task(guard._accept())
    await transport.accept_started.wait()
    task.cancel()
    try:
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        transport.pause_accept = False
        transport.release_accept.set()


async def _cancel_pending_receive(
    guard: LifecycleGuard, transport: _ModelTransport
) -> None:
    """Cancel one suspended receive without changing accepted state."""
    transport.pause_receive = True
    transport.receive_started.clear()
    transport.release_receive.clear()
    task = asyncio.create_task(guard.receive_media())
    await transport.receive_started.wait()
    task.cancel()
    try:
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        transport.pause_receive = False
        transport.release_receive.set()


@dc.dataclass(slots=True)
class _LifecycleModel:
    """Reference lifecycle phases independent of production observations."""

    connection_phase: ConnectionPhase = ConnectionPhase.PRE_ACCEPT
    session_phase: SessionPhase = SessionPhase.CONNECTING
    close_code: int | None = None
    cleanup_owners: int = 0


@dc.dataclass(slots=True)
class _OperationContext:
    """Production state and fake transport paired with a reference model."""

    guard: LifecycleGuard
    session: SessionState
    transport: _ModelTransport
    model: _LifecycleModel


async def _apply_accept(context: _OperationContext) -> None:
    if context.model.connection_phase is ConnectionPhase.PRE_ACCEPT:
        await context.guard._accept()
        context.model.connection_phase = ConnectionPhase.ACCEPTED
        context.model.session_phase = SessionPhase.ESTABLISHED
    else:
        with pytest.raises(LifecycleError):
            await context.guard._accept()


async def _apply_send(context: _OperationContext, index: int) -> None:
    if context.model.connection_phase is ConnectionPhase.ACCEPTED:
        await context.guard.send_media(f"frame-{index}")
    else:
        with pytest.raises(LifecycleError):
            await context.guard.send_media(f"frame-{index}")


async def _apply_receive(context: _OperationContext) -> None:
    if context.model.connection_phase is ConnectionPhase.ACCEPTED:
        assert await context.guard.receive_media() == "frame", (
            "accepted receives must return transport frames"
        )
    else:
        with pytest.raises(LifecycleError):
            await context.guard.receive_media()


async def _apply_peer_disconnect(context: _OperationContext) -> None:
    if context.model.connection_phase is ConnectionPhase.ACCEPTED:
        context.transport.disconnect_next = True
        with pytest.raises(_PeerDisconnectedError) as disconnect:
            await context.guard.receive_media()
        assert disconnect.value.code == 4001, (
            "peer disconnect must retain its transport code"
        )
        context.model.connection_phase = ConnectionPhase.CLOSED
        if context.model.close_code is None:
            context.model.close_code = 4001
    else:
        with pytest.raises(LifecycleError):
            await context.guard.receive_media()


async def _apply_close(context: _OperationContext, index: int) -> None:
    if context.model.connection_phase is not ConnectionPhase.CLOSED:
        close_code = 4100 + index
        await context.guard.close(code=close_code)
        context.model.connection_phase = ConnectionPhase.CLOSED
        if context.model.close_code is None:
            context.model.close_code = close_code


async def _apply_cancellation(context: _OperationContext) -> None:
    match context.model.connection_phase:
        case ConnectionPhase.PRE_ACCEPT:
            await _cancel_pending_accept(context.guard, context.transport)
        case ConnectionPhase.ACCEPTED:
            await _cancel_pending_receive(context.guard, context.transport)
        case ConnectionPhase.CLOSED:
            with pytest.raises(LifecycleError):
                await context.guard.receive_media()


async def _apply_cleanup(context: _OperationContext, index: int) -> None:
    should_own = context.model.session_phase is SessionPhase.ESTABLISHED
    owns_cleanup = context.session.begin_disconnect()
    assert owns_cleanup is should_own, (
        "only an established session may grant disconnect ownership"
    )
    context.model.cleanup_owners += int(owns_cleanup)
    if not owns_cleanup:
        return

    context.model.session_phase = SessionPhase.DISCONNECTING
    if context.model.connection_phase is not ConnectionPhase.CLOSED:
        close_code = 4200 + index
        await context.guard.close(code=close_code)
        context.model.connection_phase = ConnectionPhase.CLOSED
        if context.model.close_code is None:
            context.model.close_code = close_code
    context.session.mark_closed()
    context.model.session_phase = SessionPhase.CLOSED


async def _apply_operation(
    context: _OperationContext, operation: _Operation, index: int
) -> None:
    match operation:
        case "accept":
            await _apply_accept(context)
        case "send":
            await _apply_send(context, index)
        case "receive":
            await _apply_receive(context)
        case "peer_disconnect":
            await _apply_peer_disconnect(context)
        case "close":
            await _apply_close(context, index)
        case "cancel":
            await _apply_cancellation(context)
        case "cleanup":
            await _apply_cleanup(context, index)


def _assert_model_invariants(context: _OperationContext) -> None:
    assert context.guard.phase is context.model.connection_phase, (
        "the connection phase must match the generated reference model"
    )
    assert context.session.phase is context.model.session_phase, (
        "the session phase must match the generated reference model"
    )
    assert context.guard.close_code == context.model.close_code, (
        "later close observations must not replace the model's first code"
    )
    assert context.session.close_code == context.model.close_code, (
        "session and transport guards must agree on the first close code"
    )
    assert context.transport.successful_accepts <= 1, (
        "a connection must never complete acceptance more than once"
    )
    assert context.model.cleanup_owners <= 1, (
        "disconnect cleanup has one owner per session"
    )
    assert context.transport.data_calls_before_acceptance == 0, (
        "send and receive operations must reach the transport only after acceptance"
    )
    if context.transport.successful_accepts == 0:
        assert context.model.session_phase is SessionPhase.CONNECTING, (
            "rejected or cancelled handshakes cannot establish a session"
        )
    elif context.model.session_phase is not SessionPhase.CLOSED:
        assert context.model.session_phase is SessionPhase.ESTABLISHED, (
            "successful acceptance must establish disconnect ownership"
        )


async def _exercise_operations(operations: list[_Operation]) -> None:
    """Apply generated traces and compare production state with a reference."""
    session = SessionState()
    transport = _ModelTransport()
    context = _OperationContext(
        guard=LifecycleGuard(transport, session),
        session=session,
        transport=transport,
        model=_LifecycleModel(),
    )
    for index, operation in enumerate(operations):
        await _apply_operation(context, operation, index)
        _assert_model_invariants(context)


@settings(max_examples=60, deadline=None)
@given(
    operations=st.lists(
        st.sampled_from(_OPERATIONS),
        max_size=20,
    )
)
@example(operations=["accept", "send", "receive", "peer_disconnect", "cleanup"])
@example(operations=["cancel", "accept", "close", "cleanup"])
def test_generated_lifecycle_operations_preserve_state_invariants(
    operations: list[_Operation],
) -> None:
    """Generated operation traces preserve acceptance, data and cleanup rules."""
    asyncio.run(_exercise_operations(operations))
