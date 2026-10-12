"""Serializable, best-effort in-memory fan-out for tests on one event loop."""

from __future__ import annotations

import asyncio
import time
import typing as typ

from falcon_pachinko.fanout import FanoutTransport

if typ.TYPE_CHECKING:
    import collections.abc as cabc


class InMemoryFanoutTransport(FanoutTransport):
    """Model node leases and byte-only inboxes without sharing socket objects."""

    def __init__(self, *, clock: cabc.Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._rooms: dict[str, dict[str, float]] = {}
        self._inboxes: dict[str, asyncio.Queue[bytes | None]] = {}

    def _expire(self) -> None:
        """Forget expired memberships lazily before routing or inspection."""
        now = self._clock()
        self._rooms = {
            room: active
            for room, members in self._rooms.items()
            if (
                active := {
                    node: expiry for node, expiry in members.items() if expiry > now
                }
            )
        }

    async def subscribe(self, room: str, node_id: str, *, ttl: float) -> None:
        """Acquire a lease and ensure the node has a local inbox."""
        self._rooms.setdefault(room, {})[node_id] = self._clock() + ttl
        self._inboxes.setdefault(node_id, asyncio.Queue())

    async def unsubscribe(self, room: str, node_id: str) -> None:
        """Remove a room lease if present."""
        members = self._rooms.get(room)
        if members is not None:
            members.pop(node_id, None)
            if not members:
                self._rooms.pop(room)

    async def refresh(self, node_id: str, *, ttl: float) -> None:
        """Renew the node's live memberships without reviving expired ones."""
        self._expire()
        for members in self._rooms.values():
            if node_id in members:
                members[node_id] = self._clock() + ttl

    async def release(self, node_id: str) -> None:
        """Drop leases and close the old stream, permitting a later restart.

        Await the old listener before restarting. A new ``listen`` must start
        after ``subscribe`` or ``release`` and captures one inbox for its lifetime.
        """
        for room in tuple(self._rooms):
            await self.unsubscribe(room, node_id)
        if (inbox := self._inboxes.pop(node_id, None)) is not None:
            inbox.put_nowait(None)

    async def publish(self, room: str, envelope: bytes) -> None:
        """Queue bytes for each unexpired room member."""
        if not isinstance(envelope, bytes):
            msg = "fan-out envelope must be bytes"
            raise TypeError(msg)
        self._expire()
        for node_id in self._rooms.get(room, {}):
            self._inboxes[node_id].put_nowait(envelope)

    async def listen(self, node_id: str) -> cabc.AsyncIterator[bytes]:
        """Yield queued envelopes until the node is released."""
        inbox = self._inboxes.setdefault(node_id, asyncio.Queue())
        while (envelope := await inbox.get()) is not None:
            yield envelope

    async def members(self, room: str) -> tuple[str, ...]:
        """Inspect sorted unexpired membership without exposing queues."""
        self._expire()
        return tuple(sorted(self._rooms.get(room, {})))
