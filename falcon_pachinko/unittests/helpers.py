"""Test helper utilities for WebSocket testing."""

from __future__ import annotations

import typing as typ

if typ.TYPE_CHECKING:
    from falcon_pachinko import HookManager, WebSocketResource
else:  # pragma: no cover - runtime placeholders for type hints
    HookManager = typ.Any  # type: ignore[assignment]
    WebSocketResource = typ.Any  # type: ignore[assignment]


def bind_default_hooks(resource: WebSocketResource) -> HookManager:
    """Bind a standalone hook manager for ``resource`` during tests."""
    return resource.bind_default_hook_manager()


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
