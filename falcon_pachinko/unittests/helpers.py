"""Test helper utilities for WebSocket testing."""

from __future__ import annotations

import typing


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

    async def send_media(self, data: typing.Any) -> None:  # pragma: no cover
        """Send structured data over the connection.

        Parameters
        ----------
        data : typing.Any
            The data to send over the WebSocket connection
        """
