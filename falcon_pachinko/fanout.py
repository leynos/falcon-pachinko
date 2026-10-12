"""Serializable room fan-out, separate from process-local socket ownership.

Keep a local manager on each node and supply a byte-only transport::

    fanout = ClusterFanout("worker-a", manager, transport, lease_ttl=30.0)
    await controller.start(fanout.run)
    await fanout.add_connection(connection_id, websocket)
    await fanout.join_room(connection_id, "lobby")
    await fanout.publish("lobby", {"text": "hello"})

The worker resolves envelopes against its own registry. Applications provide
cluster-unique node and connection identifiers and stop the controller at shutdown.
"""

from __future__ import annotations

import abc
import asyncio
import dataclasses as dc
import logging
import typing as typ
import uuid

from .diagnostics import _type_name
from .dispatcher import msjson

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from .protocols import WebSocketLike
    from .websocket import WebSocketConnectionManager

logger = logging.getLogger(__name__)


@dc.dataclass(frozen=True, slots=True)
class FanoutMessage:
    """JSON envelope containing identifiers and a serializable payload."""

    message_id: str
    origin_node: str
    room: str
    payload: object
    exclude: tuple[str, ...] = ()

    def encode(self) -> bytes:
        """Serialize before publishing; unsupported payloads raise TypeError."""
        return msjson.encode(self)

    @staticmethod
    def decode(envelope: bytes) -> FanoutMessage:
        """Validate and decode a transport envelope."""
        return msjson.decode(envelope, type=FanoutMessage)


class FanoutTransport(abc.ABC):
    """Exchange serializable node leases and envelopes, never live sockets.

    Delivery is best effort, with no durability or replay to expired/down nodes.
    Per-socket ordering requires per-publisher-per-node transport ordering;
    there is no global order. At-least-once adapters may duplicate envelopes.
    Implementations must expire leases and refresh only unexpired membership.
    ``listen`` yields bytes and ``members`` returns node identifier strings.
    """

    @abc.abstractmethod
    async def subscribe(self, room: str, node_id: str, *, ttl: float) -> None:
        """Acquire or renew the node's room lease."""

    @abc.abstractmethod
    async def unsubscribe(self, room: str, node_id: str) -> None:
        """Idempotently remove the node's room lease."""

    @abc.abstractmethod
    async def refresh(self, node_id: str, *, ttl: float) -> None:
        """Renew all unexpired leases for this node."""

    @abc.abstractmethod
    async def release(self, node_id: str) -> None:
        """Remove all node leases and end its listening stream."""

    @abc.abstractmethod
    async def publish(self, room: str, envelope: bytes) -> None:
        """Deliver bytes to current members, including the publishing node."""

    @abc.abstractmethod
    def listen(self, node_id: str) -> cabc.AsyncIterator[bytes]:
        """Yield envelopes sequentially until release or transport failure."""

    @abc.abstractmethod
    async def members(self, room: str) -> tuple[str, ...]:
        """Return current, unexpired node identifiers for introspection."""


class ClusterFanout:
    """Coordinate local room membership and leased distributed notifications.

    Route all local membership changes through this coordinator. Connection
    identifiers must be cluster-unique. Publish failures propagate without
    retry; local send failures are logged without eviction or rollback.
    There is no deduplication; applications may use ``message_id`` themselves.
    Start ``run`` through WorkerController on the owning event loop.
    """

    # Explicit timing options are part of the opt-in public coordinator API.
    # pylint: disable-next=too-many-arguments  # required transport and lease configuration
    def __init__(  # ruff: ignore[too-many-arguments] - required transport and lease configuration
        self,
        node_id: str,
        manager: WebSocketConnectionManager,
        transport: FanoutTransport,
        *,
        lease_ttl: float,
        heartbeat_interval: float | None = None,
        send_timeout: float | None = None,
    ) -> None:
        self.node_id = node_id
        self.manager = manager
        self.transport = transport
        self.lease_ttl = lease_ttl
        self.heartbeat_interval = (
            lease_ttl / 3 if heartbeat_interval is None else heartbeat_interval
        )
        if not 0 < self.heartbeat_interval < lease_ttl:
            msg = "heartbeat_interval must be positive and less than lease_ttl"
            raise ValueError(msg)
        self.send_timeout = send_timeout
        self._lock = asyncio.Lock()
        self._pending_unsubscribe: set[str] = set()

    async def add_connection(self, conn_id: str, ws: WebSocketLike) -> None:
        """Register a live socket only in the local registry."""
        await self.manager.add_connection(conn_id, ws)

    async def join_room(self, conn_id: str, room: str) -> None:
        """Subscribe the node when its first local member joins."""
        async with self._lock:
            is_first = not self.manager.rooms.get(room)
            await self.manager.join_room(conn_id, room)
            if is_first:
                try:
                    await self.transport.subscribe(
                        room, self.node_id, ttl=self.lease_ttl
                    )
                except BaseException:
                    # Roll back membership even when subscription is cancelled.
                    await self.manager.leave_room(conn_id, room)
                    raise
            self._pending_unsubscribe.discard(room)

    async def leave_room(self, conn_id: str, room: str) -> None:
        """Release the node lease when its last local member leaves."""
        async with self._lock:
            await self.manager.leave_room(conn_id, room)
            if not self.manager.rooms.get(room):
                self._pending_unsubscribe.add(room)
            await self._unsubscribe_pending()

    async def remove_connection(self, conn_id: str) -> None:
        """Forget a local socket and release each emptied room lease."""
        async with self._lock:
            rooms = tuple(
                room
                for room, members in self.manager.rooms.items()
                if conn_id in members
            )
            await self.manager.remove_connection(conn_id)
            self._pending_unsubscribe.update(
                room for room in rooms if not self.manager.rooms.get(room)
            )
            await self._unsubscribe_pending()

    async def publish(
        self, room: str, payload: object, *, exclude: tuple[str, ...] = ()
    ) -> None:
        """Serialize and publish without obtaining any local or remote socket."""
        envelope = FanoutMessage(
            str(uuid.uuid4()), self.node_id, room, payload, exclude
        ).encode()
        await self.transport.publish(room, envelope)

    async def _listen(self) -> None:
        """Isolate individual malformed envelopes and local delivery failures."""
        async for envelope in self.transport.listen(self.node_id):
            try:
                message = FanoutMessage.decode(envelope)
                # The target-room snapshot discards envelopes with no local
                # members without copying unrelated rooms and memberships.
                await self.manager.broadcast_to_room(
                    message.room,
                    message.payload,
                    exclude=message.exclude,
                    timeout=self.send_timeout,
                )
            except Exception as exc:  # ruff: ignore[blind-except] - isolate delivery failures without reflecting application values
                logger.warning("Cluster fan-out delivery failed (%s)", _type_name(exc))

    async def _unsubscribe_pending(self) -> None:
        """Retry empty-room removals before refresh can prolong stale leases.

        Call only while holding the membership lock. Retain pending state on
        failure or cancellation; a later heartbeat retries it.
        """
        for room in tuple(self._pending_unsubscribe):
            if not self.manager.rooms.get(room):
                try:
                    await self.transport.unsubscribe(room, self.node_id)
                except Exception as exc:  # ruff: ignore[blind-except] - retain each failed removal for heartbeat retry
                    logger.warning(
                        "Cluster room unsubscribe failed (%s)", _type_name(exc)
                    )
                    continue
            self._pending_unsubscribe.discard(room)

    async def _renew_membership(self) -> None:
        """Restore occupied room leases after a restart or event-loop stall."""
        async with self._lock:
            await self._unsubscribe_pending()
            # Refresh renews every remote lease, including failed removals.
            # Subscribe occupied rooms individually while removals are pending.
            if not self._pending_unsubscribe:
                await self.transport.refresh(self.node_id, ttl=self.lease_ttl)
            for room, members in self.manager.rooms.items():
                if members:
                    await self.transport.subscribe(
                        room, self.node_id, ttl=self.lease_ttl
                    )

    async def _heartbeat(self) -> None:
        """Keep leases alive while the listener is running."""
        while True:
            await asyncio.sleep(self.heartbeat_interval)
            try:
                await self._renew_membership()
            except Exception as exc:  # ruff: ignore[blind-except] - transient renewal errors retry next heartbeat; cancellation propagates
                logger.warning("Cluster heartbeat failed (%s)", _type_name(exc))

    async def run(self) -> None:
        """Listen and renew leases; release membership on every exit."""
        try:
            await self._renew_membership()
            async with asyncio.TaskGroup() as tasks:
                heartbeat = tasks.create_task(self._heartbeat())
                await self._listen()
                heartbeat.cancel()
        finally:
            try:
                # A broken broker or concurrent membership call must not hang
                # shutdown. Expiry covers cleanup that cannot finish in time.
                async with asyncio.timeout(self.lease_ttl):
                    async with self._lock:
                        await self.transport.release(self.node_id)
                        self._pending_unsubscribe.clear()
            except Exception as exc:  # ruff: ignore[blind-except] - preserve the original failure or cancellation; leases expire
                logger.warning("Cluster release failed (%s)", _type_name(exc))
