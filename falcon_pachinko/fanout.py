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
import math
import typing as typ
import uuid

from ._observability import _observe_operation
from .diagnostics import _type_name
from .dispatcher import msjson

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from .protocols import WebSocketLike
    from .websocket import WebSocketConnectionManager

logger = logging.getLogger(__name__)


@dc.dataclass(frozen=True, slots=True)
class FanoutMessage:
    """JSON envelope containing identifiers and a serializable payload.

    ``exclude`` contains connection identifiers that the receiving local
    managers omit from delivery. Applications should use cluster-unique
    identifiers when coordinating connections across nodes.

    Parameters
    ----------
    message_id : str
        Unique identifier for this message, available to applications for
        deduplication.
    origin_node : str
        Cluster-unique identifier of the node that published the message.
    room : str
        Logical room used for fan-out delivery.
    payload : object
        Application data that must be JSON-serializable to encode this envelope.
    exclude : tuple[str, ...], default=()
        Connection identifiers receiving managers should omit. Use
        cluster-unique connection identifiers when connections span nodes.
    """

    message_id: str
    origin_node: str
    room: str
    payload: object
    exclude: tuple[str, ...] = ()

    def encode(self) -> bytes:
        """Encode this envelope as JSON bytes.

        Returns
        -------
        bytes
            The serialized fan-out envelope.

        Notes
        -----
        ``msjson.encode`` raises ``TypeError`` if the payload cannot be
        represented as JSON. Encode before invoking a transport so invalid
        values never cross the transport boundary.
        """
        return msjson.encode(self)

    @staticmethod
    def decode(envelope: bytes) -> FanoutMessage:
        """Decode and validate a JSON fan-out envelope.

        Parameters
        ----------
        envelope : bytes
            Serialized JSON envelope received from a transport.

        Returns
        -------
        FanoutMessage
            The decoded identifiers, exclusion list and application payload.

        Notes
        -----
        Decode and validation errors from ``msjson.decode`` propagate to the
        caller when the envelope is malformed or its fields have invalid types.
        """
        return msjson.decode(envelope, type=FanoutMessage)


class FanoutTransport(abc.ABC):
    """Exchange node leases and serialized envelopes, never live sockets.

    The wire boundary uses string room and node identifiers, floating-point
    lease durations in seconds, and byte envelopes. Delivery is best effort,
    with no durability or replay to expired or unavailable nodes. Per-socket
    ordering requires per-publisher, per-node transport ordering; there is no
    global order. At-least-once adapters may duplicate envelopes. Implementations
    must expire leases and refresh only unexpired membership. ``listen`` yields
    bytes and ``members`` returns node identifiers.
    """

    @abc.abstractmethod
    async def subscribe(self, room: str, node_id: str, *, ttl: float) -> None:
        """Acquire or renew a node's lease for one room.

        Parameters
        ----------
        room : str
            Logical room name.
        node_id : str
            Cluster-unique identifier of the node that has local room members.
        ttl : float
            Lease duration in seconds.
        """

    @abc.abstractmethod
    async def unsubscribe(self, room: str, node_id: str) -> None:
        """Idempotently remove one node's lease for a room.

        Parameters
        ----------
        room : str
            Logical room name.
        node_id : str
            Cluster-unique identifier of the node whose lease is removed.
        """

    @abc.abstractmethod
    async def refresh(self, node_id: str, *, ttl: float) -> None:
        """Renew this node's unexpired room leases.

        Parameters
        ----------
        node_id : str
            Cluster-unique identifier of the node.
        ttl : float
            New lease duration in seconds. Expired leases must not be revived
            by a refresh.
        """

    @abc.abstractmethod
    async def release(self, node_id: str) -> None:
        """Idempotently remove this node's leases and end its listener stream.

        Parameters
        ----------
        node_id : str
            Cluster-unique identifier of the node being released.
        """

    @abc.abstractmethod
    async def publish(self, room: str, envelope: bytes) -> None:
        """Attempt delivery to current room members, including the publisher.

        Parameters
        ----------
        room : str
            Logical room name used to select current, unexpired node leases.
        envelope : bytes
            Serialized ``FanoutMessage``. The transport must not accept live
            WebSocket objects at this boundary.

        Notes
        -----
        Delivery is best effort. Implementations need not persist messages or
        replay them to nodes that are unavailable when publication occurs.
        Transport-specific publication failures are propagated to the caller.
        """

    @abc.abstractmethod
    def listen(self, node_id: str) -> cabc.AsyncIterator[bytes]:
        """Return this node's envelope stream.

        Parameters
        ----------
        node_id : str
            Cluster-unique identifier of the listening node.

        Returns
        -------
        collections.abc.AsyncIterator[bytes]
            Envelopes in transport delivery order, until release or stream end.

        Notes
        -----
        Transport-specific errors may be raised while creating or consuming
        the stream.
        """

    @abc.abstractmethod
    async def members(self, room: str) -> tuple[str, ...]:
        """Return node identifiers with current room leases.

        Parameters
        ----------
        room : str
            Logical room name.

        Returns
        -------
        tuple[str, ...]
            Node identifiers whose leases for this room have not expired.
        """


class ClusterFanout:
    """Coordinate local room membership and leased distributed notifications.

    Route local membership changes through this coordinator. The manager keeps
    live sockets in the current process; the transport receives only
    serializable leases and byte envelopes. Connection identifiers must be
    cluster-unique. Publication is best effort, has no retry or deduplication,
    and may be duplicated by an at-least-once transport. Local send failures
    are logged without stopping the listener, evicting sockets or rolling back
    publication. Applications may use ``message_id`` for their own
    deduplication. Start ``run`` through ``WorkerController`` on the owning
    event loop.
    """

    # Explicit timing options are part of the opt-in public coordinator API.
    # @codescene(disable:"Excess Number of Function Arguments") #179 API; 2026-10-13
    # pylint: disable-next=too-many-arguments  # required transport and lease configuration
    def __init__(  # ruff: ignore[too-many-arguments] - required transport and lease configuration
        self,
        node_id: str,
        manager: WebSocketConnectionManager,
        transport: FanoutTransport,
        *,
        lease_ttl: float,
        heartbeat_interval: float | None = None,
        send_timeout: float | None = 5.0,
    ) -> None:
        """Configure local socket fan-out and leased cluster membership.

        Parameters
        ----------
        node_id : str
            Cluster-unique identifier for this process.
        manager : WebSocketConnectionManager
            Process-local registry that owns this node's live sockets.
        transport : FanoutTransport
            Transport for serializable node leases and byte envelopes.
        lease_ttl : float
            Room-lease duration in seconds.
        heartbeat_interval : float or None, default=None
            Lease-renewal interval in seconds. ``None`` selects one third of
            ``lease_ttl``. The selected interval must be positive and less than
            ``lease_ttl``.
        send_timeout : float or None, default=5.0
            Per-socket send timeout in seconds. A finite positive value bounds
            each send. ``None`` leaves sends unbounded; a blocked send can stall
            the sequential listener.

        Raises
        ------
        ValueError
            If the heartbeat interval is not positive and less than
            ``lease_ttl``, or if ``send_timeout`` is not finite and positive
            and is not ``None``.
        """
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
        if send_timeout is not None and not 0 < send_timeout < math.inf:
            msg = "send_timeout must be positive and finite, or None"
            raise ValueError(msg)
        self.send_timeout = send_timeout
        self._lock = asyncio.Lock()
        self._pending_unsubscribe: set[str] = set()

    async def add_connection(self, conn_id: str, ws: WebSocketLike) -> None:
        """Register a live socket in this process's connection manager.

        Parameters
        ----------
        conn_id : str
            Cluster-unique connection identifier.
        ws : WebSocketLike
            Live socket owned by this process.

        Notes
        -----
        The local manager rejects a duplicate ``conn_id`` with ``ValueError``.
        """
        await self.manager.add_connection(conn_id, ws)

    async def join_room(self, conn_id: str, room: str) -> None:
        """Add a local connection and acquire the node lease on first join.

        Parameters
        ----------
        conn_id : str
            Identifier of a connection already registered with this coordinator.
        room : str
            Logical room to join.

        Notes
        -----
        If ``conn_id`` is not registered, the local manager's
        ``WebSocketConnectionNotFoundError`` propagates.
        Transport subscription errors propagate. The local join is rolled back
        and empty-room cleanup is retained for a later retry.
        """
        async with self._lock:
            is_first = not self.manager.rooms.get(room)
            await self.manager.join_room(conn_id, room)
            if is_first:
                try:
                    with _observe_operation(logger, "subscribe"):
                        await self.transport.subscribe(
                            room, self.node_id, ttl=self.lease_ttl
                        )
                except BaseException:
                    # Roll back membership even when subscription is cancelled.
                    await self.manager.leave_room(conn_id, room)
                    # A failed acknowledgement may follow a persisted lease.
                    self._pending_unsubscribe.add(room)
                    raise
            self._pending_unsubscribe.discard(room)

    async def leave_room(self, conn_id: str, room: str) -> None:
        """Remove a local room membership and unsubscribe when the room empties.

        Parameters
        ----------
        conn_id : str
            Identifier of the local connection leaving the room.
        room : str
            Logical room to leave.

        Notes
        -----
        Failed unsubscriptions remain pending for retry by membership
        maintenance; they do not restore local room membership.
        """
        async with self._lock:
            await self.manager.leave_room(conn_id, room)
            if not self.manager.rooms.get(room):
                self._pending_unsubscribe.add(room)
            await self._unsubscribe_pending()

    async def remove_connection(self, conn_id: str) -> None:
        """Forget a local socket and clean up leases for emptied rooms.

        Parameters
        ----------
        conn_id : str
            Local connection identifier to remove.

        Notes
        -----
        Removal is idempotent. Failed unsubscriptions remain pending for
        heartbeat retry; the local socket and its memberships are still
        removed.
        """
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
        """Serialize and attempt best-effort publication to a logical room.

        Parameters
        ----------
        room : str
            Logical room to publish to.
        payload : object
            JSON-serializable application payload.
        exclude : tuple[str, ...], default=()
            Connection identifiers omitted by receiving local managers.

        Notes
        -----
        If ``payload`` cannot be encoded as JSON, ``FanoutMessage.encode``
        raises ``TypeError`` before a transport call.
        Transport publication errors propagate without retry.
        """
        with _observe_operation(logger, "publish"):
            envelope = FanoutMessage(
                str(uuid.uuid4()), self.node_id, room, payload, exclude
            ).encode()
            await self.transport.publish(room, envelope)

    async def _listen(self) -> None:
        """Isolate individual malformed envelopes and local delivery failures."""
        async for envelope in self.transport.listen(self.node_id):
            try:
                with _observe_operation(logger, "decode", emit_success=False):
                    message = FanoutMessage.decode(envelope)
                # The target-room snapshot discards envelopes with no local
                # members without copying unrelated rooms and memberships.
                with _observe_operation(logger, "deliver", emit_success=False):
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
                    with _observe_operation(logger, "unsubscribe"):
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
                with _observe_operation(logger, "refresh"):
                    await self.transport.refresh(self.node_id, ttl=self.lease_ttl)
            for room, members in self.manager.rooms.items():
                if members:
                    try:
                        with _observe_operation(logger, "subscribe"):
                            await self.transport.subscribe(
                                room, self.node_id, ttl=self.lease_ttl
                            )
                    except Exception as exc:  # ruff: ignore[blind-except] - retry this room on the next heartbeat while renewing later rooms
                        logger.warning(
                            "Cluster room subscribe failed (%s)", _type_name(exc)
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
        """Listen for envelopes, renew leases and release membership on exit.

        Start this coroutine with ``WorkerController`` in the application's
        ASGI lifespan. Startup or listener transport failures propagate to the
        worker owner. Transient heartbeat errors are logged and retried on the
        next interval. Cancellation propagates after bounded release cleanup;
        if release fails, lease expiry remains the fallback.

        """
        try:
            await self._renew_membership()
            heartbeat = asyncio.create_task(self._heartbeat())
            try:
                with _observe_operation(logger, "listen"):
                    await self._listen()
            finally:
                # Await the listener directly so its original transport error
                # reaches WorkerController without TaskGroup wrapping.
                heartbeat.cancel()
                await asyncio.gather(heartbeat, return_exceptions=True)
        finally:
            try:
                # A broken broker or concurrent membership call must not hang
                # shutdown. Expiry covers cleanup that cannot finish in time.
                with _observe_operation(logger, "release"):
                    async with asyncio.timeout(self.lease_ttl):
                        async with self._lock:
                            await self.transport.release(self.node_id)
                            self._pending_unsubscribe.clear()
            except Exception as exc:  # ruff: ignore[blind-except] - preserve the original failure or cancellation; leases expire
                logger.warning("Cluster release failed (%s)", _type_name(exc))
