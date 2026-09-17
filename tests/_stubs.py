"""Shared test doubles used by the unit and behaviour suites."""

from __future__ import annotations

import types
import typing as typ

from falcon_pachinko.websocket import (
    ConnectionBackend,
    WebSocketConnectionNotFoundError,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from falcon_pachinko.protocols import WebSocketLike


class RecordingWebSocket:
    """WebSocket stub that records lifecycle changes and sent payloads."""

    def __init__(self) -> None:
        self.accepted = False
        self.closed = False
        self.close_code: int | None = None
        self.messages: list[object] = []
        self.receive_calls = 0

    async def accept(self, subprotocol: str | None = None) -> None:
        """Record that the handshake was accepted."""
        self.accepted = True

    # pylint: disable-next=trivial-attribute-wrapper  # protocol stub: WebSocketLike fixes send_media's shape and recording the payload is this stub's entire purpose
    async def send_media(self, data: object) -> None:
        """Record data sent through the stub."""
        self.messages.append(data)

    async def close(self, code: int = 1000) -> None:
        """Record closure metadata."""
        self.closed = True
        self.close_code = code

    async def receive_media(self) -> object:
        """Record the receive attempt and return a placeholder payload."""
        self.receive_calls += 1
        return None


class RequestStub:
    """Minimal request double exposing headers and routing metadata."""

    def __init__(self, path: str, headers: dict[str, str]) -> None:
        self.path = path
        self.path_template = "/ws"
        self._headers = {key.lower(): value for key, value in headers.items()}

    def get_header(self, name: str, default: str | None = None) -> str | None:
        """Return the recorded header value for ``name`` if present."""
        return self._headers.get(name.lower(), default)


class RecordingBackend(ConnectionBackend):
    """Custom backend that records calls for assertion."""

    def __init__(self) -> None:
        self._websockets: dict[str, WebSocketLike] = {}
        self._rooms: dict[str, set[str]] = {}
        self.calls: list[str] = []

    @property
    def websockets(self) -> cabc.Mapping[str, WebSocketLike]:
        """Expose a read-only snapshot of active websockets."""
        return types.MappingProxyType(self._websockets.copy())

    @property
    def rooms(self) -> cabc.Mapping[str, cabc.Collection[str]]:
        """Expose a read-only snapshot of room memberships."""
        snapshot = {room: set(ids) for room, ids in self._rooms.items()}
        return types.MappingProxyType(snapshot)

    async def add_connection(self, conn_id: str, ws: WebSocketLike) -> None:
        """Record a connection registration."""
        self.calls.append(f"add_connection:{conn_id}")
        if conn_id in self._websockets:
            msg = f"Duplicate connection ID: {conn_id!r}"
            raise ValueError(msg)
        self._websockets[conn_id] = ws

    async def remove_connection(self, conn_id: str) -> None:
        """Forget a connection and purge empty rooms."""
        self.calls.append(f"remove_connection:{conn_id}")
        self._websockets.pop(conn_id, None)
        for members in self._rooms.values():
            members.discard(conn_id)
        self._rooms = {room: ids for room, ids in self._rooms.items() if ids}

    async def join_room(self, conn_id: str, room: str) -> None:
        """Associate a connection with a room."""
        self.calls.append(f"join_room:{conn_id}:{room}")
        if conn_id not in self._websockets:
            raise WebSocketConnectionNotFoundError(conn_id)
        self._rooms.setdefault(room, set()).add(conn_id)

    async def leave_room(self, conn_id: str, room: str) -> None:
        """Remove a connection from ``room`` if present."""
        self.calls.append(f"leave_room:{conn_id}:{room}")
        members = self._rooms.get(room)
        if members is None:
            return
        members.discard(conn_id)
        if not members:
            self._rooms.pop(room, None)

    async def get_websocket(self, conn_id: str) -> WebSocketLike | None:
        """Return the websocket for ``conn_id`` when known."""
        self.calls.append(f"get_websocket:{conn_id}")
        return self._websockets.get(conn_id)

    async def snapshot(
        self, room: str | None = None
    ) -> list[tuple[str, WebSocketLike]]:
        """Return a snapshot of members for a room or all connections."""
        label = room if room is not None else "*"
        self.calls.append(f"snapshot:{label}")
        if room is None:
            return list(self._websockets.items())
        members = self._rooms.get(room, set())
        return [
            (cid, self._websockets[cid]) for cid in members if cid in self._websockets
        ]
