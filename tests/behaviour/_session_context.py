"""Focused live session _session_context.py support."""

from __future__ import annotations

import asyncio
import dataclasses as dc
import typing as typ
from contextlib import AsyncExitStack

if typ.TYPE_CHECKING:
    from websockets.exceptions import ConnectionClosed

    from falcon_pachinko.testing import WebSocketTestClient
    from falcon_pachinko.testing.client import WebSocketSession
    from tests.behaviour._live_server import DisconnectRecorder, LiveServer

_ASSERTION_FAILURE = "lifecycle expectation failed"


@dc.dataclass(slots=True)
class LiveScenario:
    """Hold a real server and client connection for one BDD scenario."""

    event_loop: asyncio.AbstractEventLoop
    server: LiveServer
    client: WebSocketTestClient
    recorder: DisconnectRecorder
    client_contexts: AsyncExitStack | None = None
    session: WebSocketSession | None = None
    observed_close_code: int | None = None
    rejected_handshake: Exception | None = None


async def connect(context: LiveScenario, path: str = "/ws/session") -> None:
    """Open the production router's mounted session through the test client."""
    if context.client_contexts is None:
        context.client_contexts = AsyncExitStack()
    manager = context.client.connect(path)
    context.session = await asyncio.wait_for(
        context.client_contexts.enter_async_context(manager), timeout=5
    )


async def close_client_context(context: LiveScenario) -> None:
    """Release the client context after server-side lifecycle work has ended."""
    stack = context.client_contexts
    if stack is not None:
        await asyncio.wait_for(stack.aclose(), timeout=5)
        context.client_contexts = None
        context.session = None


async def finish_client_session(context: LiveScenario) -> None:
    """End a live client session and wait for its resource cleanup."""
    assert context.session is not None, _ASSERTION_FAILURE
    await asyncio.wait_for(context.session.close(4000), timeout=5)
    conn_id = context.recorder.connection_ids[-1]
    await context.recorder.wait_for_disconnect(conn_id)


async def stop_server(context: LiveScenario) -> None:
    """Stop Uvicorn before exact-once assertions observe final state."""
    await context.server.stop()
    await close_client_context(context)


def close_code_from_error(error: ConnectionClosed) -> int:
    """Extract the peer's close code from a websockets client error."""
    received = error.rcvd
    if received is not None:
        return received.code
    return error.code
