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
