"""Shared live lifecycle fixtures and cross-scenario assertions."""

from __future__ import annotations

import asyncio
import typing as typ

import pytest
from pytest_bdd import given, then

from falcon_pachinko.testing import WebSocketTestClient
from tests.behaviour._live_server import LiveServer
from tests.behaviour._session_context import (
    LiveScenario,
    close_client_context,
    stop_server,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc


@pytest.fixture
def live_event_loop(
    event_loop_policy: asyncio.AbstractEventLoopPolicy,
) -> cabc.Iterator[asyncio.AbstractEventLoop]:
    """Give Uvicorn and the test client one event loop for their whole session."""
    loop = event_loop_policy.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        yield loop
    finally:
        asyncio.set_event_loop(None)
        loop.close()


@given("a live Falcon ASGI WebSocket server", target_fixture="context")
def given_live_server(
    live_event_loop: asyncio.AbstractEventLoop,
    request: pytest.FixtureRequest,
) -> LiveScenario:
    """Start Falcon through Uvicorn and prepare the client and recorder."""
    event_loop = live_event_loop
    server = event_loop.run_until_complete(LiveServer.start(event_loop))
    client = WebSocketTestClient(server.base_url, allow_insecure=True)
    context = LiveScenario(
        event_loop=event_loop,
        server=server,
        client=client,
        recorder=server.application.recorder,
    )

    def cleanup() -> None:
        async def finish() -> None:
            if not context.server._stopped:
                await context.server.stop()
            await close_client_context(context)

        event_loop.run_until_complete(finish())

    request.addfinalizer(cleanup)
    return context


@then("disconnect cleanup is recorded once with code 4000")
def then_client_close_recorded(context: LiveScenario) -> None:
    """Stop the server and verify the peer's code reached cleanup once."""
    context.event_loop.run_until_complete(stop_server(context))
    records = context.recorder.disconnect_records
    assert len(records) == 1, "one accepted client must disconnect exactly once"
    assert records[0][1] == 4000, "cleanup must receive the client's close code"


@then("the manager has no connection")
def then_cancelled_membership_removed(context: LiveScenario) -> None:
    """Verify cancellation cannot strand accepted manager membership."""
    assert context.server.application.manager.websockets == {}, (
        "server stop must remove accepted membership"
    )
