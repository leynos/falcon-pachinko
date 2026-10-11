"""Generated histories compare leased fan-out with an independent room model.

The fake clock controls expiry, while long real heartbeat intervals keep the
history deterministic. Restart exercises public worker lease reacquisition;
the named heartbeat tests separately cover periodic scheduling and failures.
"""

from __future__ import annotations

import asyncio
import dataclasses as dc
import typing as typ

from hypothesis import example, given, settings
from hypothesis import strategies as st

from falcon_pachinko import ClusterFanout, FanoutMessage, WebSocketConnectionManager
from falcon_pachinko.testing import InMemoryFanoutTransport
from tests._stubs import RecordingWebSocket

if typ.TYPE_CHECKING:
    import collections.abc as cabc

_NODES = ("a", "b")
_ROOMS = ("lobby", "garden")
_LEASE_TTL = 1000.0


@dc.dataclass(frozen=True, slots=True)
class _Operation:
    """One shrinkable command with bounded node, connection, and room choices."""

    action: str
    node: int = 0
    slot: int = 0
    room: int = 0
    exclude: bool = False


@dc.dataclass(slots=True)
class _Model:
    """Track logical membership and lease deadlines without transport internals."""

    now: float = 0.0
    connections: dict[str, set[str]] = dc.field(default_factory=dict)
    leases: dict[tuple[str, str], float] = dc.field(default_factory=dict)

    def occupied(self, node_id: str, room: str) -> bool:
        """Return whether a node owns any logical member of this room."""
        return any(
            conn_id.startswith(f"{node_id}:") and room in rooms
            for conn_id, rooms in self.connections.items()
        )

    def members(self, room: str) -> tuple[str, ...]:
        """Compute visible nodes from absolute deadlines, including exact expiry."""
        return tuple(
            node_id
            for node_id in _NODES
            if self.leases.get((node_id, room), self.now) > self.now
        )

    def refresh(self, node_id: str) -> None:
        """Extend live deadlines without reviving expired room membership."""
        for room in _ROOMS:
            if node_id in self.members(room):
                self.leases[node_id, room] = self.now + _LEASE_TTL


class _ObservedTransport(InMemoryFanoutTransport):
    """Record bytes and signal listener startup without inspecting its queues."""

    def __init__(self, model: _Model) -> None:
        """Attach the model clock and prepare independent listener readiness."""
        super().__init__(clock=lambda: model.now)
        self.ready = {node_id: asyncio.Event() for node_id in _NODES}
        self.published: list[bytes] = []

    @typ.override
    async def publish(self, room: str, envelope: bytes) -> None:
        """Check the serialized boundary before forwarding each publication."""
        assert isinstance(envelope, bytes), "only serialized envelopes may cross"
        assert FanoutMessage.decode(envelope).room == room, "room must match envelope"
        self.published.append(envelope)
        await super().publish(room, envelope)

    @typ.override
    async def listen(self, node_id: str) -> cabc.AsyncIterator[bytes]:
        """Notify startup and verify every value crossing the receiving boundary."""
        self.ready[node_id].set()
        async for envelope in super().listen(node_id):
            assert isinstance(envelope, bytes), "listeners must receive bytes"
            yield envelope


class _ProbeSocket(RecordingWebSocket):
    """Expose a FIFO delivery acknowledgement for the worker's probe room."""

    def __init__(self) -> None:
        """Create an acknowledgement queue for this node's FIFO barrier."""
        super().__init__()
        self.received: asyncio.Queue[object] = asyncio.Queue()

    @typ.override
    async def send_media(self, data: object) -> None:
        """Acknowledge delivery after earlier messages on this listener finish."""
        await super().send_media(data)
        self.received.put_nowait(data)


@dc.dataclass(slots=True)
class _Node:
    """Keep each worker and its delivery barrier separate from model state."""

    fanout: ClusterFanout
    probe: _ProbeSocket = dc.field(default_factory=_ProbeSocket)
    task: asyncio.Task[None] | None = None


class _History:
    """Execute commands against two nodes and compare observable results."""

    def __init__(self) -> None:
        """Create isolated coordinators, sockets, and oracle state for one history."""
        self.model = _Model()
        self.transport = _ObservedTransport(self.model)
        self.nodes = {
            node_id: _Node(
                ClusterFanout(
                    node_id,
                    WebSocketConnectionManager(),
                    self.transport,
                    lease_ttl=_LEASE_TTL,
                    heartbeat_interval=_LEASE_TTL / 2,
                )
            )
            for node_id in _NODES
        }
        self.sockets: dict[str, RecordingWebSocket] = {}
        # Retain removed socket instances too, so later accidental sends fail.
        self.expected: dict[RecordingWebSocket, list[object]] = {}
        self.sequence = 0

    async def start(self, node_id: str) -> None:
        """Start a released worker and model reacquisition of occupied rooms."""
        node = self.nodes[node_id]
        if node.task is not None:
            return
        self.transport.ready[node_id].clear()
        node.task = asyncio.create_task(node.fanout.run())
        async with asyncio.timeout(1):
            await self.transport.ready[node_id].wait()
        for room in _ROOMS:
            if self.model.occupied(node_id, room):
                self.model.leases[node_id, room] = self.model.now + _LEASE_TTL

    async def stop(self, node_id: str) -> None:
        """Release twice and require the public worker stream to finish."""
        node = self.nodes[node_id]
        await self.transport.release(node_id)
        if node.task is not None:
            async with asyncio.timeout(1):
                await node.task
            node.task = None
        await self.transport.release(node_id)
        for room in _ROOMS:
            self.model.leases.pop((node_id, room), None)

    async def drain(self) -> None:
        """Use FIFO probe envelopes to await all prior deliveries without sleeps."""
        for node_id, node in self.nodes.items():
            if node.task is None:
                continue
            assert not node.task.done(), "the active listener must survive commands"
            # Probe leases are outside generated rooms and may themselves expire.
            probe_room = f"delivery-barrier-{node_id}"
            await self.transport.subscribe(probe_room, node_id, ttl=_LEASE_TTL)
            token = {"barrier": self.sequence, "node": node_id}
            self.sequence += 1
            await node.fanout.publish(probe_room, token)
            async with asyncio.timeout(1):
                assert await node.probe.received.get() == token, (
                    "the probe must acknowledge the current FIFO barrier"
                )

    async def check(self) -> None:
        """Check lease visibility, exact local membership, and all socket histories."""
        await self.drain()
        for node_id, node in self.nodes.items():
            expected_connections = {
                conn_id
                for conn_id in self.model.connections
                if conn_id.startswith(f"{node_id}:")
            } | {f"probe-{node_id}"}
            assert set(node.fanout.manager.websockets) == expected_connections, (
                "each registry must contain exactly its own registered connections"
            )
        for room in _ROOMS:
            await self.check_room(room)
        for socket, messages in self.expected.items():
            assert socket.messages == messages, "deliveries must match the room model"

    async def check_room(self, room: str) -> None:
        """Compare one room's visible leases and each node's logical members."""
        assert await self.transport.members(room) == self.model.members(room), (
            "transport membership must match independent lease deadlines"
        )
        for node_id, node in self.nodes.items():
            expected = {
                conn_id
                for conn_id, rooms in self.model.connections.items()
                if conn_id.startswith(f"{node_id}:") and room in rooms
            }
            actual = set(node.fanout.manager.rooms.get(room, ()))
            assert actual == expected, "local membership must match logical joins"
            if not expected:
                assert node_id not in await self.transport.members(room), (
                    "successful last-member cleanup must remove the room lease"
                )

    async def membership(self, operation: _Operation) -> None:
        """Apply legal registrations and idempotent local membership removals."""
        node_id = _NODES[operation.node]
        node = self.nodes[node_id]
        conn_id = f"{node_id}:{operation.slot}"
        room = _ROOMS[operation.room]
        rooms = self.model.connections.get(conn_id)
        match operation.action:
            case "add" if rooms is None and node.task is not None:
                socket = RecordingWebSocket()
                await node.fanout.add_connection(conn_id, socket)
                self.sockets[conn_id] = socket
                self.expected[socket] = []
                self.model.connections[conn_id] = set()
            case "join" if rooms is not None and node.task is not None:
                was_empty = not self.model.occupied(node_id, room)
                await node.fanout.join_room(conn_id, room)
                rooms.add(room)
                if was_empty:
                    self.model.leases[node_id, room] = self.model.now + _LEASE_TTL
            case "leave":
                await node.fanout.leave_room(conn_id, room)
                if rooms is not None:
                    rooms.discard(room)
                if not self.model.occupied(node_id, room):
                    self.model.leases.pop((node_id, room), None)
            case "remove":
                await self.remove(node_id, conn_id)

    async def remove(self, node_id: str, conn_id: str) -> None:
        """Remove registration and every room lease left without local members."""
        await self.nodes[node_id].fanout.remove_connection(conn_id)
        self.model.connections.pop(conn_id, None)
        self.sockets.pop(conn_id, None)
        for room in _ROOMS:
            if not self.model.occupied(node_id, room):
                self.model.leases.pop((node_id, room), None)

    async def publish(self, operation: _Operation) -> None:
        """Predict local and remote deliveries before publishing serialized data."""
        room = _ROOMS[operation.room]
        excluded = (
            tuple(f"{node_id}:{operation.slot}" for node_id in _NODES)
            if operation.exclude
            else ()
        )
        payload = {"publication": self.sequence, "room": room}
        self.sequence += 1
        visible = self.model.members(room)
        for conn_id, rooms in self.model.connections.items():
            owner = conn_id.split(":")[0]
            if room not in rooms or conn_id in excluded:
                continue
            if owner not in visible or self.nodes[owner].task is None:
                continue
            self.expected[self.sockets[conn_id]].append(payload)
        await self.nodes[_NODES[operation.node]].fanout.publish(
            room, payload, exclude=excluded
        )

    async def apply(self, operation: _Operation) -> None:
        """Execute one generated command and compare every observable invariant."""
        node_id = _NODES[operation.node]
        match operation.action:
            case "add" | "join" | "leave" | "remove":
                await self.membership(operation)
            case "advance":
                self.model.now += (_LEASE_TTL / 2, _LEASE_TTL)[operation.slot]
            case "refresh":
                await self.transport.refresh(node_id, ttl=_LEASE_TTL)
                self.model.refresh(node_id)
            case "release":
                await self.stop(node_id)
            case "restart":
                await self.stop(node_id)
                await self.start(node_id)
            case "publish":
                await self.publish(operation)
        await self.check()

    async def run(self, operations: list[_Operation]) -> None:
        """Isolate each generated history and bound worker teardown on failure."""
        try:
            for node_id, node in self.nodes.items():
                await node.fanout.add_connection(f"probe-{node_id}", node.probe)
                await node.fanout.join_room(
                    f"probe-{node_id}", f"delivery-barrier-{node_id}"
                )
                await self.start(node_id)
            # Start with useful cross-node membership; generated tails explore
            # changes instead of spending most examples on absent connections.
            for index in range(len(_NODES)):
                await self.membership(_Operation("add", node=index))
                await self.membership(_Operation("join", node=index))
            await self.check()
            for operation in operations:
                async with asyncio.timeout(3):
                    await self.apply(operation)
            assert self.transport.published, "FIFO probes must cross the byte boundary"
        finally:
            for node_id, node in self.nodes.items():
                if node.task is not None:
                    node.task.cancel()
                    async with asyncio.timeout(1):
                        await asyncio.gather(node.task, return_exceptions=True)
                await self.transport.release(node_id)


_OPERATIONS = st.builds(
    _Operation,
    action=st.sampled_from((
        "add",
        "join",
        "leave",
        "remove",
        "advance",
        "refresh",
        "publish",
        "release",
        "restart",
    )),
    node=st.integers(min_value=0, max_value=1),
    slot=st.integers(min_value=0, max_value=1),
    room=st.integers(min_value=0, max_value=1),
    exclude=st.booleans(),
)


@example(
    operations=[
        _Operation("publish"),
        _Operation("publish", exclude=True),
        _Operation("advance", slot=1),
        _Operation("refresh"),
        _Operation("publish"),
        _Operation("restart"),
        _Operation("publish"),
        _Operation("leave"),
        _Operation("leave"),
        _Operation("remove", node=1),
        _Operation("remove", node=1),
        _Operation("release"),
        _Operation("release"),
        _Operation("restart"),
    ]
)
@example(
    operations=[
        _Operation("add", slot=1),
        _Operation("join", slot=1),
        _Operation("join", room=1),
        _Operation("advance"),
        _Operation("refresh"),
        _Operation("advance"),
        _Operation("publish"),
        _Operation("publish", exclude=True),
        _Operation("remove"),
        _Operation("publish"),
    ]
)
@settings(deadline=None)
@given(operations=st.lists(_OPERATIONS, min_size=1, max_size=24))
def test_generated_room_membership_lease_and_delivery_histories(
    operations: list[_Operation],
) -> None:
    """Generated histories preserve leases, cleanup, exclusions, and local delivery."""
    asyncio.run(_History().run(operations))
