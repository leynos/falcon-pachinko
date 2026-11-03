"""Tests for native socket lifecycle state adaptation."""

from __future__ import annotations

import typing as typ
from unittest import mock

import falcon.asgi
import pytest

from falcon_pachinko._session import SessionState
from falcon_pachinko._session_socket import SessionWebSocket

if typ.TYPE_CHECKING:
    from falcon_pachinko.protocols import WebSocketLike


def test_supported_falcon_receive_seam_is_callable() -> None:
    """Raw frame dispatch depends on Falcon's private receive seam."""
    assert callable(getattr(falcon.asgi.WebSocket, "_receive", None)), (
        "supported Falcon versions must expose the raw receive coroutine"
    )


@pytest.mark.asyncio
async def test_failed_native_close_restores_open_socket_state() -> None:
    """A failed close must not leave the receive guard permanently closed."""
    close_error = RuntimeError("close failed")
    native = mock.Mock(spec=falcon.asgi.WebSocket)
    native.unaccepted = False
    native.closed = False
    native.close = mock.AsyncMock(side_effect=[close_error, None])
    websocket = SessionWebSocket(
        typ.cast("WebSocketLike", native),
        typ.cast("falcon.asgi.WebSocket", native),
        SessionState(),
    )

    with pytest.raises(RuntimeError, match="close failed") as raised:
        await websocket.close()

    assert raised.value is close_error, "the original transport failure must escape"
    assert not websocket.closed, "the still-open socket must remain usable"

    await websocket.close()

    assert websocket.closed, "a later successful close must guard the socket"
