"""Test helper utilities for WebSocket testing."""

from __future__ import annotations

import asyncio
import collections.abc as cabc
import types
import typing as typ
from collections import deque

import falcon


def _is_frame_collection(value: object) -> typ.TypeGuard[cabc.Iterable[object]]:
    """Identify iterable frame batches without treating one frame as a batch."""
    match value:
        case str() | bytes() | bytearray() | memoryview() | cabc.Mapping():
            return False
        case cabc.Iterable():
            return True
        case _:
            return False


def make_req(path: str, path_template: str = "") -> types.SimpleNamespace:
    """Build a minimal request stand-in for router tests.

    Parameters
    ----------
    path : str
        The request path presented to the router
    path_template : str, optional
        The mount prefix template, by default ""

    Returns
    -------
    types.SimpleNamespace
        An object exposing ``path`` and ``path_template`` attributes
    """
    return types.SimpleNamespace(path=path, path_template=path_template)


class DummyWS:
    """A dummy WebSocket implementation for testing purposes."""

    def __init__(self, *frames: object, disconnect_code: int = 1000) -> None:
        """Initialize a socket with frames followed by a peer disconnect.

        Parameters
        ----------
        *frames : object
            Frames queued before the disconnect. If the only argument is an
            iterable other than a frame payload such as text, bytes, or a
            mapping, its items are expanded into separate frames. A list or
            tuple therefore cannot be passed as one frame through this
            constructor.
        disconnect_code : int, optional
            Close code reported by the queued peer disconnect, by default 1000.
        """
        if len(frames) == 1:
            frame_batch = frames[0]
            if _is_frame_collection(frame_batch):
                frames = tuple(frame_batch)
        self._frames = deque(frames)
        self._disconnect_code = disconnect_code
        self._closed = False
        self._close_code: int | None = None
        self._accepted = False
        self.receive_calls = 0
        self.lifecycle_event = asyncio.Event()

    @property
    def unaccepted(self) -> bool:
        """Match Falcon's public handshake state."""
        return not self._accepted

    @property
    def close_code(self) -> int | None:
        """The first close code, if the socket has closed."""
        return self._close_code

    @property
    def is_closed(self) -> bool:
        """Expose closed state without taking the recording field's name."""
        return self._closed

    async def accept(self, subprotocol: str | None = None) -> None:  # pragma: no cover
        """Accept the WebSocket handshake.

        Parameters
        ----------
        subprotocol : str or None, optional
            The WebSocket subprotocol to use, by default None
        """
        self._accepted = True
        self.lifecycle_event.set()

    async def close(self, code: int = 1000) -> None:  # pragma: no cover
        """Close the WebSocket connection.

        Parameters
        ----------
        code : int, optional
            The WebSocket close code, by default 1000
        """
        if self._closed:
            return
        self._closed = True
        self._close_code = code
        self.lifecycle_event.set()

    async def send_media(self, data: object) -> None:  # pragma: no cover
        """Send structured data over the connection.

        Parameters
        ----------
        data : object
            The data to send over the WebSocket connection
        """

    async def receive_media(self) -> object:
        """Return the next scripted frame or raise a disconnect event."""
        self.receive_calls += 1
        if self._frames:
            frame = self._frames.popleft()
            if isinstance(frame, BaseException):
                raise frame
            return frame
        code = self._close_code if self._closed else self._disconnect_code
        raise falcon.WebSocketDisconnected(code)


class RecordingWS(DummyWS):
    """A dummy WebSocket that records accept and close calls for assertions."""

    def __init__(self, *frames: object, disconnect_code: int = 1000) -> None:
        """Initialize scripted frames and empty call logs."""
        super().__init__(*frames, disconnect_code=disconnect_code)
        self.accepted: list[str | None] = []
        self.closed: list[int] = []
        self.sent: list[object] = []
        self.accepted_event = asyncio.Event()

    @typ.override
    async def accept(self, subprotocol: str | None = None) -> None:
        """Record the accepted subprotocol.

        Parameters
        ----------
        subprotocol : str or None, optional
            The WebSocket subprotocol to use, by default None
        """
        self.accepted.append(subprotocol)
        await super().accept(subprotocol)
        self.accepted_event.set()

    @typ.override
    async def send_media(self, data: object) -> None:
        """Record media sent through the socket."""
        self.sent.append(data)

    @typ.override
    async def close(self, code: int = 1000) -> None:
        """Record the close code.

        Parameters
        ----------
        code : int, optional
            The WebSocket close code, by default 1000
        """
        if not self.is_closed:
            self.closed.append(code)
        await super().close(code)
