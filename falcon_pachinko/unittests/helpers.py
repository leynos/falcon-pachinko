"""Test helper utilities for WebSocket testing."""

from __future__ import annotations

import types
import typing as typ


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

    async def send_media(self, data: object) -> None:  # pragma: no cover
        """Send structured data over the connection.

        Parameters
        ----------
        data : object
            The data to send over the WebSocket connection
        """

    @staticmethod
    async def receive_media() -> object:  # pragma: no cover
        """Receive structured data over the connection."""
        return None


class RecordingWS(DummyWS):
    """A dummy WebSocket that records accept and close calls for assertions."""

    def __init__(self) -> None:
        """Initialize empty call logs for accept and close."""
        self.accepted: list[str | None] = []
        self.closed: list[int] = []

    @typ.override
    async def accept(
        self, subprotocol: str | None = None
    ) -> None:
        """Record the accepted subprotocol.

        Parameters
        ----------
        subprotocol : str or None, optional
            The WebSocket subprotocol to use, by default None
        """
        self.accepted.append(subprotocol)

    @typ.override
    async def close(
        self, code: int = 1000
    ) -> None:
        """Record the close code.

        Parameters
        ----------
        code : int, optional
            The WebSocket close code, by default 1000
        """
        self.closed.append(code)
