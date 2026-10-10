"""Test helper utilities for WebSocket testing."""

from __future__ import annotations

import asyncio
import collections.abc as cabc
import types
import typing as typ
from collections import deque

import falcon


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

    def __init__(
        self,
        frames: cabc.Iterable[object] = (),
        *,
        disconnect_code: int = 1000,
    ) -> None:
        """Initialize a socket with frames followed by a peer disconnect."""
        self._frames = deque(frames)
        self._disconnect_code = disconnect_code
        self._closed = False
        self._close_code: int | None = None
        self.receive_calls = 0

    async def accept(self, subprotocol: str | None = None) -> None:  # pragma: no cover
        """Accept the WebSocket handshake.

        Parameters
        ----------
        subprotocol : str or None, optional
            The WebSocket subprotocol to use, by default None
        """

    async def close(self, code: int = 1000) -> None:  # pragma: no cover
        """Close the WebSocket connection.

        Parameters
        ----------
        code : int, optional
            The WebSocket close code, by default 1000
        """
        self._closed = True
        self._close_code = code

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
        if self._closed:
            raise falcon.WebSocketDisconnected(self._close_code)
        if self._frames:
            return self._frames.popleft()
        raise falcon.WebSocketDisconnected(self._disconnect_code)


class RecordingWS(DummyWS):
    """A dummy WebSocket that records accept and close calls for assertions."""

    def __init__(
        self,
        frames: cabc.Iterable[object] = (),
        *,
        disconnect_code: int = 1000,
    ) -> None:
        """Initialize scripted frames and empty call logs."""
        super().__init__(frames, disconnect_code=disconnect_code)
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
        self.closed.append(code)
        await super().close(code)
