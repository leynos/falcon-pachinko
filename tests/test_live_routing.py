"""Live Falcon ASGI coverage for WebSocket route-template matching."""

from __future__ import annotations

import asyncio
import contextlib
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
        self.identity = identity
        self.observations = observations
        observations.factories.append(identity)

    async def on_connect(
        self, req: falcon.Request, ws: WebSocketLike, **params: object
    ) -> bool:
        self.observations.selected.append(self.identity)
        return True


_RecordingResource.hooks = HookCollection()


def _record_resource_hook(context: HookContext) -> None:
    resource = typ.cast("_RecordingResource", context.target)
    resource.observations.hooks.append(f"{resource.identity}.{context.event}")


_RecordingResource.hooks.add("before_connect", _record_resource_hook)
_RecordingResource.hooks.add("after_connect", _record_resource_hook)


class _RoutingAdapter:
    """Forward Falcon's live request to Pachinko and identify its selection."""

    def __init__(
        self, router: WebSocketRouter, observations: _LiveObservations
    ) -> None:
        self._router = router
        self._observations = observations

    async def on_websocket(
        self, req: falcon.asgi.Request, ws: falcon.asgi.WebSocket, rest: str
    ) -> None:
        self._observations.adapter_calls.append((req.path, rest))
        await self._router.on_websocket(
            _RouterRequest(path=req.path, path_template="/ws"),
            # Both APIs use send_media positionally; only its parameter name differs.
            typ.cast("WebSocketLike", ws),
        )
        identity = self._observations.selected[-1]
        await ws.send_media(identity)


@pytest_asyncio.fixture
async def live_routing_server() -> cabc.AsyncIterator[tuple[str, _LiveObservations]]:
    """Serve Pachinko behind Falcon on a pre-bound loopback socket."""
    observations = _LiveObservations([], [], [], [])
    router = WebSocketRouter()

    def record_global_hook(context: HookContext) -> None:
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

    app = falcon.asgi.App()
    app.add_route("/ws/{rest:path}", _RoutingAdapter(router, observations))

    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host="127.0.0.1",
            port=0,
            lifespan="off",
            log_level="critical",
            access_log=False,
        )
    )
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server_task: asyncio.Task[None] | None = None

    try:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        # A zero timeout puts the listening socket in non-blocking mode.
        listener.settimeout(0.0)
        port = listener.getsockname()[1]
        server_task = asyncio.create_task(server.serve(sockets=[listener]))

        async with asyncio.timeout(5):
            while not server.started:
                if server_task.done():
                    await server_task
                    msg = "Uvicorn stopped before reporting startup"
                    raise RuntimeError(msg)
                await asyncio.sleep(0.01)

        yield f"ws://127.0.0.1:{port}", observations
    finally:
        try:
            if server_task is not None:
                server.should_exit = True
                try:
                    async with asyncio.timeout(5):
                        await server_task
                except TimeoutError:
                    server_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await server_task
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
