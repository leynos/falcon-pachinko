"""Live two-server proof of serializable fan-out and local socket ownership."""

from __future__ import annotations

import asyncio
import dataclasses as dc
import types
import typing as typ
import uuid
from contextlib import AsyncExitStack, asynccontextmanager

import falcon.asgi
import pytest
from pytest_bdd import given, scenario, then, when
from websockets.exceptions import ConnectionClosed

from falcon_pachinko import (
    ClusterFanout,
    FanoutTransport,
    InProcessBackend,
    WebSocketConnectionManager,
    WebSocketResource,
    WebSocketRouter,
    WorkerController,
    configure_raw_frame_media,
)
from falcon_pachinko.testing import InMemoryFanoutTransport, WebSocketTestClient
from tests._live_server import live_server

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from falcon_pachinko import WebSocketLike
    from falcon_pachinko.testing.client import WebSocketSession


class RecordingTransport(FanoutTransport):
    """Record boundary types without accepting any socket registry reference."""

    def __init__(self) -> None:
        """Create an observable proxy around the shared byte transport."""
        self.inner = InMemoryFanoutTransport()
        self.observed: list[tuple[str, type[object]]] = []
        self.ready = {"a": asyncio.Event(), "b": asyncio.Event()}
        self.changed = asyncio.Event()

    def _record(self, operation: str, *values: object) -> None:
        """Record every protocol argument or yielded primitive value."""
        self.observed.extend((operation, type(value)) for value in values)

    @typ.override
    async def subscribe(self, room: str, node_id: str, *, ttl: float) -> None:
        """Record serializable lease arguments."""
        self._record("subscribe", room, node_id, ttl)
        await self.inner.subscribe(room, node_id, ttl=ttl)

    @typ.override
    async def unsubscribe(self, room: str, node_id: str) -> None:
        """Remove membership and wake cleanup observers."""
        self._record("unsubscribe", room, node_id)
        await self.inner.unsubscribe(room, node_id)
        self.changed.set()

    @typ.override
    async def refresh(self, node_id: str, *, ttl: float) -> None:
        """Record lease renewal arguments."""
        self._record("refresh", node_id, ttl)
        await self.inner.refresh(node_id, ttl=ttl)

    @typ.override
    async def release(self, node_id: str) -> None:
        """Release node metadata and its byte stream."""
        self._record("release", node_id)
        await self.inner.release(node_id)
        self.changed.set()

    @typ.override
    async def publish(self, room: str, envelope: bytes) -> None:
        """Route only a logical room name and encoded bytes."""
        self._record("publish", room, envelope)
        await self.inner.publish(room, envelope)

    @typ.override
    async def listen(self, node_id: str) -> cabc.AsyncIterator[bytes]:
        """Record byte deliveries and provide an explicit worker readiness signal."""
        self._record("listen", node_id)
        self.ready[node_id].set()
        async for envelope in self.inner.listen(node_id):
            self._record("listen.yield", envelope)
            yield envelope

    @typ.override
    async def members(self, room: str) -> tuple[str, ...]:
        """Record the serializable identifiers returned by introspection."""
        self._record("members", room)
        members = await self.inner.members(room)
        self._record("members.result", *members)
        return members


class SocketRecordingBackend(InProcessBackend):
    """Track identities of sockets registered and accessed by one server."""

    def __init__(self) -> None:
        """Initialize per-server identity sets for ownership assertions."""
        super().__init__()
        self.registered: set[int] = set()
        self.snapshotted: set[int] = set()
        self.looked_up: set[int] = set()

    @typ.override
    async def add_connection(self, conn_id: str, ws: WebSocketLike) -> None:
        """Record successful local registrations."""
        await super().add_connection(conn_id, ws)
        self.registered.add(id(ws))

    @typ.override
    async def snapshot(
        self, room: str | None = None
    ) -> list[tuple[str, WebSocketLike]]:
        """Record every socket returned by local snapshots."""
        result = await super().snapshot(room)
        self.snapshotted.update(id(ws) for _, ws in result)
        return result

    @typ.override
    async def get_websocket(self, conn_id: str) -> WebSocketLike | None:
        """Record every direct local lookup, including future send paths."""
        ws = await super().get_websocket(conn_id)
        if ws is not None:
            self.looked_up.add(id(ws))
        return ws


class RoomResource(WebSocketResource):
    """Join a logical room locally and publish through byte-only fan-out."""

    def __init__(self, fanout: ClusterFanout) -> None:
        """Bind one resource instance to its node's fan-out coordinator."""
        self.fanout = fanout
        self.conn_id = f"{fanout.node_id}:{uuid.uuid4()}"
        self.room = ""

    @typ.override
    async def on_connect(
        self,
        req: falcon.Request,
        ws: WebSocketLike,
        **params: object,
    ) -> bool:
        """Register this server's socket and acquire its logical room lease."""
        self.room = str(params["room"])
        await self.fanout.add_connection(self.conn_id, ws)
        await self.fanout.join_room(self.conn_id, self.room)
        return True

    async def on_publish(self, ws: object, body: object) -> None:
        """Publish the client payload without accessing any remote socket."""
        await self.fanout.publish(self.room, body)

    @typ.override
    async def on_disconnect(self, ws: WebSocketLike, close_code: int) -> None:
        """Release local membership after a real ASGI disconnect."""
        await self.fanout.remove_connection(self.conn_id)


class RouterMount:
    """Adapt Falcon's greedy mount to Pachinko's explicit mount prefix."""

    def __init__(self, router: WebSocketRouter) -> None:
        """Keep the mounted router available to Falcon's ASGI adapter."""
        self.router = router

    async def on_websocket(
        self,
        req: falcon.asgi.Request,
        ws: falcon.asgi.WebSocket,
        rest: str,
    ) -> None:
        """Route the real server-owned socket with a mount-relative request."""
        await self.router.on_websocket(
            types.SimpleNamespace(path=req.path, path_template="/ws"),
            # Falcon's media parameter name differs; its runtime surface matches.
            typ.cast("WebSocketLike", ws),
        )


@dc.dataclass(slots=True)
class Node:
    """Hold independently mounted application state for one server."""

    node_id: str
    app: falcon.asgi.App
    backend: SocketRecordingBackend
    manager: WebSocketConnectionManager
    fanout: ClusterFanout


def make_node(node_id: str, transport: RecordingTransport) -> Node:
    """Create a node with an independent local registry and receive-loop router."""
    backend = SocketRecordingBackend()
    manager = WebSocketConnectionManager(backend)
    fanout = ClusterFanout(node_id, manager, transport, lease_ttl=3.0, send_timeout=1.0)
    router = WebSocketRouter(receive_loop=True)
    router.add_route("/rooms/{room}", RoomResource, fanout)
    router.mount("/ws")
    app = falcon.asgi.App()
    configure_raw_frame_media(app)
    app.add_route("/ws/{rest:path}", RouterMount(router))
    return Node(node_id, app, backend, manager, fanout)


@asynccontextmanager
async def running_node(
    node: Node, transport: RecordingTransport
) -> cabc.AsyncIterator[None]:
    """Start fan-out through WorkerController and await listener readiness."""
    controller = WorkerController()
    await controller.start(node.fanout.run)
    try:
        async with asyncio.timeout(3):
            await transport.ready[node.node_id].wait()
        yield
    finally:
        await controller.stop()


@dc.dataclass(slots=True)
class ClusterContext:
    """Own live servers, workers, and client sessions on a dedicated scenario loop."""

    loop: asyncio.AbstractEventLoop
    stack: AsyncExitStack
    transport: RecordingTransport
    nodes: tuple[Node, Node]
    urls: list[str] = dc.field(default_factory=list)
    clients: list[WebSocketTestClient] = dc.field(default_factory=list)
    sessions: list[WebSocketSession] = dc.field(default_factory=list)


@pytest.fixture
def event_loop(
    event_loop_policy: asyncio.AbstractEventLoopPolicy,
) -> cabc.Iterator[asyncio.AbstractEventLoop]:
    """Use a dedicated scenario event loop, closed only after owned resources."""
    loop = event_loop_policy.new_event_loop()
    try:
        asyncio.set_event_loop(loop)
        yield loop
    finally:
        loop.run_until_complete(loop.shutdown_asyncgens())
        asyncio.set_event_loop(None)
        loop.close()


@pytest.fixture
def cluster_context(
    event_loop: asyncio.AbstractEventLoop,
) -> cabc.Iterator[ClusterContext]:
    """Enter servers then workers so sessions close before workers and servers."""
    transport = RecordingTransport()
    context = ClusterContext(
        event_loop,
        AsyncExitStack(),
        transport,
        (make_node("a", transport), make_node("b", transport)),
    )

    async def start() -> None:
        """Start both servers before their worker listeners and sessions."""
        for node in context.nodes:
            url = await context.stack.enter_async_context(live_server(node.app))
            context.urls.append(url)
            context.clients.append(WebSocketTestClient(url, allow_insecure=True))
        for node in context.nodes:
            await context.stack.enter_async_context(running_node(node, transport))

    try:
        event_loop.run_until_complete(start())
        yield context
    finally:
        event_loop.run_until_complete(context.stack.aclose())


@scenario(
    "cluster_fanout.feature", "A room spans two independently mounted live servers"
)
def test_live_cluster_fanout() -> None:
    """Register the real-server cluster fan-out acceptance scenario."""


@given(
    "clients A and B connect to separate live servers in room lobby",
    target_fixture="context",
)
def connect_clients(cluster_context: ClusterContext) -> ClusterContext:
    """Keep both real WebSocket sessions open across scenario steps."""

    async def connect() -> None:
        """Open one real client session against each mounted server."""
        for client in cluster_context.clients:
            session = await cluster_context.stack.enter_async_context(
                client.connect("/ws/rooms/lobby")
            )
            cluster_context.sessions.append(session)

    cluster_context.loop.run_until_complete(connect())
    for node in cluster_context.nodes:
        assert len(node.manager.websockets) == 1, (
            "each server must own one independently accepted socket"
        )
    assert cluster_context.urls[0] != cluster_context.urls[1], (
        "clients must use independent server ports"
    )
    return cluster_context


@then("lobby has both node memberships")
def both_members(context: ClusterContext) -> None:
    """Both independent servers must subscribe to the same logical room."""
    members = context.loop.run_until_complete(context.transport.members("lobby"))
    assert members == ("a", "b"), "both node identifiers must hold room leases"


@when("client A publishes the first message")
def publish_first(context: ClusterContext) -> None:
    """Initiate publish through client A's real server receive loop."""
    context.loop.run_until_complete(
        context.sessions[0].send_json({"type": "publish", "payload": {"text": "first"}})
    )


@then("both clients receive the first message")
def receive_first(context: ClusterContext) -> None:
    """Real clients on distinct sockets receive the shared logical publish."""

    async def receive() -> None:
        """Collect each client's first payload within its own timeout."""
        for session in context.sessions:
            async with asyncio.timeout(3):
                message = await session.receive_json()
            assert message == {"text": "first"}, (
                "both servers must deliver the published payload"
            )

    context.loop.run_until_complete(receive())


@then("only serializable values cross the fan-out transport")
def serializable_boundary(context: ClusterContext) -> None:
    """Verify every boundary value is primitive metadata or bytes."""
    assert context.transport.observed, (
        "the scenario must exercise the transport boundary"
    )
    assert {kind for _, kind in context.transport.observed} <= {str, float, bytes}, (
        "no live socket or application object may cross the transport"
    )
    assert (
        sum(operation == "listen.yield" for operation, _ in context.transport.observed)
        == 2
    ), "each node must receive a byte envelope"


@then("each server uses only its own live socket objects")
def local_socket_ownership(context: ClusterContext) -> None:
    """Prove snapshot and lookup ownership with recording registries."""
    first, second = (node.backend for node in context.nodes)
    assert first.registered.isdisjoint(second.registered), (
        "socket identities must be disjoint across servers"
    )
    for backend in (first, second):
        assert backend.snapshotted == backend.registered, (
            "each server must have delivered using its own socket"
        )
        assert backend.looked_up <= backend.registered, (
            "no server may obtain the other server socket"
        )


@when("client B disconnects")
def disconnect_second(context: ClusterContext) -> None:
    """Close client B through its actual WebSocket connection."""
    context.loop.run_until_complete(context.sessions[1].close())


@then("lobby retains only node A")
def only_first_member(context: ClusterContext) -> None:
    """Poll metadata with a bound while real disconnect cleanup completes."""

    async def wait_for_cleanup() -> None:
        """Wait for unsubscribe notifications until only node A remains."""
        async with asyncio.timeout(3):
            while True:
                context.transport.changed.clear()
                if await context.transport.members("lobby") == ("a",):
                    break
                await context.transport.changed.wait()

    context.loop.run_until_complete(wait_for_cleanup())
    assert not context.nodes[1].manager.websockets, (
        "client B must be removed from its local registry"
    )
    assert len(context.nodes[0].manager.websockets) == 1, (
        "client A must remain connected"
    )


@when("client A publishes the second message")
def publish_second(context: ClusterContext) -> None:
    """Publish again after remote disconnect cleanup."""
    context.loop.run_until_complete(
        context.sessions[0].send_json({
            "type": "publish",
            "payload": {"text": "second"},
        })
    )


@then("client A receives the second message and client B receives nothing")
def receive_after_disconnect(context: ClusterContext) -> None:
    """Remote cleanup leaves the other real client and its delivery path intact."""

    async def receive() -> None:
        """Verify A receives the publish and B receives no later frame."""
        async with asyncio.timeout(3):
            message = await context.sessions[0].receive_json()
        assert message == {"text": "second"}, (
            "the remaining client must still receive publishes"
        )
        with pytest.raises((ConnectionClosed, TimeoutError)):
            async with asyncio.timeout(0.2):
                await context.sessions[1].receive_json()

    context.loop.run_until_complete(receive())
