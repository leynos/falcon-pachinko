"""Test helper utilities for WebSocket testing."""

from __future__ import annotations

import types
import typing as typ

import falcon

if typ.TYPE_CHECKING:
    import collections.abc as cabc


class DummyRequest(types.SimpleNamespace):
    """Retain flexible test attributes while declaring the router's path fields."""

    path: str
    path_template: str


def make_req(path: str, path_template: str = "") -> DummyRequest:
    """Build a minimal request stand-in for router tests.

    Parameters
    ----------
    path : str
        The request path presented to the router
    path_template : str, optional
        The mount prefix template, by default ""

    Returns
    -------
    DummyRequest
        An object exposing ``path`` and ``path_template`` attributes
    """
    return DummyRequest(path=path, path_template=path_template)


class DummyWS:
    """A dummy WebSocket implementation for testing purposes."""

    def __init__(self, *frames: object) -> None:
        self.unaccepted = True
        self._frames = list(frames)

    async def accept(
        self,
        subprotocol: str | None = None,
        headers: cabc.Mapping[str, str] | cabc.Iterable[tuple[str, str]] | None = None,
    ) -> None:  # pragma: no cover
        """Accept the WebSocket handshake.

        Parameters
        ----------
        subprotocol : str or None, optional
            The WebSocket subprotocol to use, by default None
        """
        del headers
        self.unaccepted = False

    @staticmethod
    async def close(
        code: int | None = 1000, reason: str | None = None
    ) -> None:  # pragma: no cover
        """Close the WebSocket connection.

        Parameters
        ----------
        code : int, optional
            The WebSocket close code, by default 1000
        """
        del code, reason

    @staticmethod
    async def send_media(media: object) -> None:  # pragma: no cover
        """Send structured data over the connection.

        Parameters
        ----------
        media : object
            The data to send over the WebSocket connection
        """
        del media

    async def send_text(self, message: str) -> None:  # pragma: no cover
        """Send a text frame through the media test seam."""
        await self.send_media(message)

    async def receive_media(self) -> object:  # pragma: no cover
        """Receive a seeded frame or report the simulated client close."""
        if self._frames:
            frame = self._frames.pop(0)
            if isinstance(frame, BaseException):
                raise frame
            return frame
        raise falcon.WebSocketDisconnected(1000)

    async def receive_text(self) -> str:
        """Receive one seeded frame and require its payload to be text."""
        message = await self.receive_media()
        if isinstance(message, str):
            return message
        msg = "expected a text WebSocket frame"
        raise TypeError(msg)


class RecordingWS(DummyWS):
    """A dummy WebSocket that records accept and close calls for assertions."""

    def __init__(self, *frames: object) -> None:
        """Initialize empty call logs for accept and close."""
        super().__init__(*frames)
        self.accepted: list[str | None] = []
        self.accept_calls: list[
            tuple[
                str | None,
                cabc.Mapping[str, str] | cabc.Iterable[tuple[str, str]] | None,
            ]
        ] = []
        self.closed: list[int] = []

    @typ.override
    async def accept(
        self,
        subprotocol: str | None = None,
        headers: cabc.Mapping[str, str] | cabc.Iterable[tuple[str, str]] | None = None,
    ) -> None:
        """Record the accepted subprotocol.

        Parameters
        ----------
        subprotocol : str or None, optional
            The WebSocket subprotocol to use, by default None
        """
        self.accepted.append(subprotocol)
        self.accept_calls.append((subprotocol, headers))
        self.unaccepted = False

    @typ.override
    async def close(self, code: int | None = 1000, reason: str | None = None) -> None:
        """Record the close code.

        Parameters
        ----------
        code : int, optional
            The WebSocket close code, by default 1000
        """
        del reason
        self.closed.append(1000 if code is None else code)
