"""Common types, constants, and exceptions for testing utilities."""

from __future__ import annotations

import typing as typ

Direction = typ.Literal["send", "receive", "close", "error"]
FrameKind = typ.Literal["text", "bytes", "json"]
PayloadKind = typ.Literal["text", "bytes", "json", "close"]

_MISSING_WEBSOCKETS_MSG = (
    "WebSocketTestClient requires the 'websockets' package. Install "
    "falcon-pachinko[testing] to enable these helpers."
)
_EXPECTED_TEXT_MSG = "Expected text frame but received bytes"
_EXPECTED_BYTES_MSG = "Expected binary frame but received text"
_TEXT_PAYLOAD_REQUIRED_MSG = "Text frames require str payloads"
_BINARY_PAYLOAD_REQUIRED_MSG = "Binary frames require bytes payloads"
_UNSUPPORTED_FRAME_KIND_MSG = "Unsupported frame kind: {frame_kind}"
_FAILED_JSON_DECODE_MSG = "Failed to decode JSON payload: {message!r}"
_JSON_FRAME_REQUIRED_MSG = "JSON frames must be text or binary payloads"
_ORIGINAL_WS_RECEIVE_MSG = "Original websocket stub does not support receiving frames"
_INSECURE_WEBSOCKET_MSG = (
    "Insecure websocket URLs require allow_insecure=True. "
    "Use a wss:// URL for secure connections."
)


class MissingDependencyError(RuntimeError):
    """Raised when optional testing dependencies are unavailable."""


class _LifecycleSocket:
    """Track websocket lifecycle state and mirror events to an optional peer."""

    def __init__(self) -> None:
        self._accepted = False
        self._closed = False
        self._close_code: int | None = None
        self._subprotocol: str | None = None
        self._peer: _LifecycleSocket | None = None

    def bind_peer(self, peer: _LifecycleSocket) -> None:
        """Mirror lifecycle events to ``peer`` when accepting or closing."""
        self._peer = peer

    @property
    def accepted(self) -> bool:
        """Return ``True`` once :meth:`accept` has been invoked."""
        return self._accepted

    @property
    def closed(self) -> bool:
        """Return ``True`` once :meth:`close` has been invoked."""
        return self._closed

    @property
    def close_code(self) -> int | None:
        """Return the close code provided to :meth:`close`, if any."""
        return self._close_code

    @property
    def subprotocol(self) -> str | None:
        """Return the negotiated subprotocol, if any."""
        return self._subprotocol

    async def accept(self, subprotocol: str | None = None) -> None:
        """Record handshake acceptance and mirror to any bound peer."""
        if self._accepted:
            return
        self._accepted = True
        self._subprotocol = subprotocol
        peer = self._peer
        if peer is not None and not peer.accepted:
            await peer.accept(subprotocol=subprotocol)

    async def close(self, code: int = 1000) -> None:
        """Record connection closure and mirror to any bound peer."""
        if self._closed:
            return
        self._closed = True
        self._close_code = code
        peer = self._peer
        if peer is not None and not peer.closed:
            await peer.close(code)
