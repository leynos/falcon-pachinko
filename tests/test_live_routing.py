"""Live Falcon ASGI coverage for WebSocket route-template matching."""

from __future__ import annotations

import asyncio
import dataclasses as dc
import socket
import typing as typ

import falcon
import falcon.asgi
import pytest
import pytest_asyncio
import uvicorn
from websockets.exceptions import InvalidStatusCode

from falcon_pachinko import (
    HookCollection,
    HookContext,
    WebSocketResource,
    WebSocketRouter,
)
from falcon_pachinko.testing import WebSocketTestClient

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from falcon_pachinko.protocols import WebSocketLike


@dc.dataclass(slots=True)
class _LiveObservations:
    """Record dispatch, construction, selection and hook activity."""

    adapter_calls: list[tuple[str, str]]
    factories: list[str]
    selected: list[str]
    hooks: list[str]


@dc.dataclass(frozen=True, slots=True)
class _RouterRequest:
    """Expose the request fields consumed by the mounted router."""

    path: str
    path_template: str


class _RecordingResource(WebSocketResource):
    """Identify the resource selected by a live WebSocket connection."""

    def __init__(self, identity: str, observations: _LiveObservations) -> None:
        """Record which live route factory the matcher selected."""
        self.identity = identity
        self.observations = observations
        observations.factories.append(identity)

    async def on_connect(
        self, req: falcon.Request, ws: WebSocketLike, **params: object
    ) -> bool:
        """Record the selected live resource and accept the connection."""
        self.observations.selected.append(self.identity)
        return True


_RecordingResource.hooks = HookCollection()


def _record_resource_hook(context: HookContext) -> None:
    """Record resource-specific hook order during live dispatch."""
    resource = typ.cast("_RecordingResource", context.target)
    resource.observations.hooks.append(f"{resource.identity}.{context.event}")


_RecordingResource.hooks.add("before_connect", _record_resource_hook)
_RecordingResource.hooks.add("after_connect", _record_resource_hook)


class _RoutingAdapter:
    """Forward Falcon's live request to Pachinko and identify its selection."""

    def __init__(
        self, router: WebSocketRouter, observations: _LiveObservations
    ) -> None:
        """Hold the Pachinko router and live-dispatch observations."""
        self._router = router
        self._observations = observations

    async def on_websocket(
        self, req: falcon.asgi.Request, ws: falcon.asgi.WebSocket, rest: str
    ) -> None:
        """Forward the Falcon request and report the selected resource."""
        self._observations.adapter_calls.append((req.path, rest))
        await self._router.on_websocket(
            _RouterRequest(path=req.path, path_template="/ws"),
            # Both APIs use send_media positionally; only its parameter name differs.
            typ.cast("WebSocketLike", ws),
        )
        identity = self._observations.selected[-1]
        await ws.send_media(identity)


def _create_live_router(observations: _LiveObservations) -> WebSocketRouter:
    """Register live routes and recording hooks on a mounted router."""
    router = WebSocketRouter()

    def record_global_hook(context: HookContext) -> None:
        """Record global hook order around the resource-specific chain."""
        observations.hooks.append(f"global.{context.event}")

    router.global_hooks.add("before_connect", record_global_hook)
    router.global_hooks.add("after_connect", record_global_hook)

    routes = (
        ("/child.v1", "dotted"),
        ("/child/v1", "slashed"),
        ("/plus+route", "plus"),
        ("/paren(route)", "parentheses"),
        ("/star*route", "star"),
    )
    for path, identity in routes:
        router.add_route(path, _RecordingResource, identity, observations)
    router.mount("/ws")
    return router


class _ReadyServer(uvicorn.Server):
    """Signal once Uvicorn has created its listening server."""

    def __init__(self, config: uvicorn.Config, started_event: asyncio.Event) -> None:
        """Retain the event that signals the listening socket is ready."""
        super().__init__(config)
        self._started_event = started_event

    async def startup(self, sockets: list[socket.socket] | None = None) -> None:
        """Signal test clients after Uvicorn finishes server startup."""
        await super().startup(sockets=sockets)
        self._started_event.set()


async def _wait_for_server_start(
    started_event: asyncio.Event, server_task: asyncio.Task[None]
) -> None:
    """Wait briefly for Uvicorn to start or fail."""
    readiness_task = asyncio.create_task(started_event.wait())
    try:
        done, _ = await asyncio.wait(
            {readiness_task, server_task},
            timeout=5,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if readiness_task in done:
            return
        if server_task in done:
            await server_task
            msg = "Uvicorn stopped before reporting startup"
            raise RuntimeError(msg)
        msg = "Uvicorn did not report startup before the timeout"
        raise TimeoutError(msg)
    finally:
        readiness_task.cancel()
        await asyncio.gather(readiness_task, return_exceptions=True)


async def _stop_server(server: uvicorn.Server, server_task: asyncio.Task[None]) -> None:
    """Request a graceful shutdown and bound any server cleanup wait."""
    server.should_exit = True
    if server_task.done():
        return

    try:
        await asyncio.wait_for(asyncio.shield(server_task), timeout=5)
    except TimeoutError:
        server_task.cancel()
        await asyncio.gather(server_task, return_exceptions=True)


@pytest_asyncio.fixture
async def live_routing_server() -> cabc.AsyncIterator[tuple[str, _LiveObservations]]:
    """Serve Pachinko behind Falcon on a pre-bound loopback socket."""
    observations = _LiveObservations([], [], [], [])
    router = _create_live_router(observations)
    app = falcon.asgi.App()
    app.add_route("/ws/{rest:path}", _RoutingAdapter(router, observations))

    started_event = asyncio.Event()
    server = _ReadyServer(
        uvicorn.Config(
            app,
            host="127.0.0.1",
            port=0,
            lifespan="off",
            log_level="critical",
            access_log=False,
        ),
        started_event,
    )
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        # A zero timeout puts the listening socket in non-blocking mode.
        listener.settimeout(0.0)
        port = listener.getsockname()[1]
        server_task = asyncio.create_task(server.serve(sockets=[listener]))
        try:
            await _wait_for_server_start(started_event, server_task)
            yield f"ws://127.0.0.1:{port}", observations
        finally:
            await _stop_server(server, server_task)
    finally:
        listener.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("path", "identity"),
    [
        ("/ws/child.v1", "dotted"),
        ("/ws/child/v1", "slashed"),
        ("/ws/plus+route", "plus"),
        ("/ws/paren(route)", "parentheses"),
        ("/ws/star*route", "star"),
    ],
)
async def test_live_websocket_routes_select_literal_resource_and_hooks(
    live_routing_server: tuple[str, _LiveObservations],
    path: str,
    identity: str,
) -> None:
    """Real Falcon handshakes preserve literal routes and hook ownership."""
    base_url, observations = live_routing_server
    client = WebSocketTestClient(base_url, allow_insecure=True)

    async with client.connect(path) as session:
        message = await session.receive_json()

    assert message == identity, "the live handshake must reach its intended resource"
    assert observations.adapter_calls == [(path, path.removeprefix("/ws/"))], (
        "Falcon must dispatch the real path to the Pachinko adapter"
    )
    assert observations.factories == [identity], (
        "only the matching route resource should be constructed"
    )
    assert observations.selected == [identity], (
        "only the matching route resource should accept the connection"
    )
    assert observations.hooks == [
        "global.before_connect",
        f"{identity}.before_connect",
        f"{identity}.after_connect",
        "global.after_connect",
    ], "global and resource hooks must unwind around the selected resource"


@pytest.mark.asyncio
async def test_live_near_miss_reaches_adapter_without_resource_activity(
    live_routing_server: tuple[str, _LiveObservations],
) -> None:
    """Falcon dispatches a near-miss that Pachinko rejects before selection."""
    base_url, observations = live_routing_server
    client = WebSocketTestClient(base_url, allow_insecure=True)

    with pytest.raises(InvalidStatusCode):
        async with client.connect("/ws/childxv1"):
            pytest.fail("a literal near-miss must not complete the handshake")

    assert observations.adapter_calls == [("/ws/childxv1", "childxv1")], (
        "the broad Falcon route must pass the near-miss to Pachinko"
    )
    assert not observations.factories, "a near-miss must not construct a resource"
    assert not observations.selected, "a near-miss must not select a resource"
    assert not observations.hooks, "a near-miss must not run resource or global hooks"
