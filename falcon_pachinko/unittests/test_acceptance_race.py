"""Concurrency tests for the guarded WebSocket handshake."""

from __future__ import annotations

import asyncio

import falcon
import pytest

from falcon_pachinko._session import SessionState
from falcon_pachinko._session_socket import _peer_disconnect_error
from falcon_pachinko.lifecycle import (
    LifecycleError,
    LifecycleGuard,
    _LifecycleGuardBindings,
)


class _SuspendedAcceptanceWebSocket:
    """Transport whose accept call remains in flight until released."""

    def __init__(self) -> None:
        """Create the events that expose and release an in-flight acceptance."""
        self.accept_calls = 0
        self.accept_started = asyncio.Event()
        self.release_accept = asyncio.Event()
        self.unaccepted = True
        self.closed = False

    async def accept(
        self, subprotocol: str | None = None, headers: object = None
    ) -> None:
        """Record the transport call, then wait before completing it."""
        del subprotocol, headers
        self.accept_calls += 1
        self.accept_started.set()
        await self.release_accept.wait()
        self.unaccepted = False

    async def close(self, code: int | None = 1000, reason: str | None = None) -> None:
        """Close the fake transport."""
        del code, reason
        self.closed = True

    async def send_media(self, data: object) -> None:
        """Consume one outgoing media value."""
        del data

    async def receive_media(self) -> object:
        """Return one placeholder media value."""
        return None

    async def send_text(self, message: str) -> None:
        """Consume one outgoing text value."""
        del message

    async def receive_text(self) -> str:
        """Return a placeholder text value."""
        return ""


class _ServerCloseRaceWebSocket(_SuspendedAcceptanceWebSocket):
    """Expose a peer reply while a server close is still completing."""

    def __init__(self, session: SessionState) -> None:
        """Retain lifecycle state and coordinate close with receive."""
        super().__init__()
        self.unaccepted = False
        self.session = session
        self.close_started = asyncio.Event()
        self.release_close = asyncio.Event()

    async def close(self, code: int | None = 1000, reason: str | None = None) -> None:
        """Publish the server code before awaiting the close handshake."""
        del reason
        if self.session.close_code is None:
            self.session.close_code = 1000 if code is None else code
        self.close_started.set()
        await self.release_close.wait()
        self.closed = True

    async def receive_media(self) -> object:
        """Report the default-code peer acknowledgement during server close."""
        await self.close_started.wait()
        raise falcon.WebSocketDisconnected(1000)


@pytest.mark.asyncio
async def test_overlapping_acceptance_reaches_transport_once() -> None:
    """A pending handshake reserves acceptance before yielding."""
    websocket = _SuspendedAcceptanceWebSocket()
    guard = LifecycleGuard(websocket, SessionState())

    async def accept_with_warning() -> None:
        """Start the compatibility handshake while checking its warning."""
        with pytest.warns(
            DeprecationWarning, match="WebSocket acceptance is router-owned"
        ):
            await guard.accept()

    first = asyncio.create_task(accept_with_warning())
    await websocket.accept_started.wait()

    second_attempted = asyncio.Event()

    async def accept_again() -> None:
        """Expose the overlapping attempt before it reaches the guard."""
        second_attempted.set()
        with pytest.warns(
            DeprecationWarning, match="WebSocket acceptance is router-owned"
        ):
            await guard.accept()

    second = asyncio.create_task(accept_again())
    await second_attempted.wait()
    await asyncio.sleep(0)
    transport_calls_before_release = websocket.accept_calls

    with pytest.raises(LifecycleError, match="before WebSocket acceptance"):
        await guard.send_text("too early")

    websocket.release_accept.set()
    first_result, second_result = await asyncio.gather(
        first, second, return_exceptions=True
    )

    assert first_result is None, "the first acceptance should complete"
    assert isinstance(second_result, LifecycleError), (
        "the overlapping acceptance should fail after the first succeeds"
    )
    assert transport_calls_before_release == 1, (
        "only one acceptance may reach the transport while it is pending"
    )
    assert websocket.accept_calls == 1, "the transport must accept once"


@pytest.mark.asyncio
async def test_peer_close_reply_preserves_server_selected_close_code() -> None:
    """A default peer reply cannot replace the server's selected close code."""
    session = SessionState()
    websocket = _ServerCloseRaceWebSocket(session)
    guard = LifecycleGuard(
        websocket,
        session,
        bindings=_LifecycleGuardBindings(disconnect_classifier=_peer_disconnect_error),
    )

    close_task = asyncio.create_task(guard.close(4002))
    await websocket.close_started.wait()
    with pytest.raises(falcon.WebSocketDisconnected):
        await guard.receive_media()
    websocket.release_close.set()
    await close_task

    assert session.close_code == 4002, "the session must retain its selected code"
    assert guard.close_code == 4002, "the guard must retain its selected code"
