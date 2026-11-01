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
        self.accepted: list[str | None] = []
        self.accepted_event = asyncio.Event()
        self.accept_calls: list[
            tuple[
                str | None,
                cabc.Mapping[str, str] | cabc.Iterable[tuple[str, str]] | None,
            ]
        ] = []

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

    async def accept(
        self,
        subprotocol: str | None = None,
        headers: cabc.Mapping[str, str] | cabc.Iterable[tuple[str, str]] | None = None,
    ) -> None:  # pragma: no cover
        """Accept once and retain the handshake options.

        Parameters
        ----------
        subprotocol : str or None, optional
            Requested WebSocket subprotocol, by default None.
        headers : Mapping[str, str] or Iterable[tuple[str, str]] or None, optional
            Handshake headers recorded in ``accept_calls``, by default None.
        """
        if self._accepted or self._closed:
            return
        self.accepted.append(subprotocol)
        self.accept_calls.append((subprotocol, headers))
        self._accepted = True
        self.accepted_event.set()
        self.lifecycle_event.set()

    async def close(
        self, code: int | None = 1000, reason: str | None = None
    ) -> None:  # pragma: no cover
        """Close once and retain the transport's effective close code.

        Parameters
        ----------
        code : int or None, optional
            Close code to retain, defaulting to 1000.
        reason : str or None, optional
            Close reason. The stub ignores this value, by default None.
        """
        del reason
        if self._closed:
            return
        self._closed = True
        self._close_code = 1000 if code is None else code
        self.lifecycle_event.set()

    async def send_media(self, data: object, /) -> None:  # pragma: no cover
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

    async def send_text(self, message: str) -> None:  # pragma: no cover
        """Send one text frame through the media test seam."""
        await self.send_media(message)

    async def receive_text(self) -> str:
        """Receive one frame and require its payload to be text."""
        message = await self.receive_media()
        if isinstance(message, str):
            return message
        msg = "expected a text WebSocket frame"
        raise TypeError(msg)


class RecordingWS(DummyWS):
    """A dummy WebSocket that records sent frames and close calls."""

    def __init__(self, *frames: object, disconnect_code: int = 1000) -> None:
        """Initialize scripted frames and empty call logs."""
        super().__init__(*frames, disconnect_code=disconnect_code)
        self.closed: list[int] = []
        self.sent: list[object] = []

    @typ.override
    async def send_media(self, data: object) -> None:
        """Record media sent through the socket."""
        self.sent.append(data)

    @typ.override
    async def close(self, code: int | None = 1000, reason: str | None = None) -> None:
        """Record the close code.

        Parameters
        ----------
        code : int, optional
            The WebSocket close code, by default 1000
        """
        effective_code = 1000 if code is None else code
        if not self.is_closed:
            self.closed.append(effective_code)
        await super().close(effective_code, reason)
