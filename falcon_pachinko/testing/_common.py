"""Common types, constants, and exceptions for testing utilities."""

from __future__ import annotations

import typing as typ

import msgspec as ms

from falcon_pachinko.diagnostics import _class_name, _frame_metadata, _type_name

if typ.TYPE_CHECKING:
    import msgspec.json as msjson

type Direction = typ.Literal["send", "receive", "close", "error"]
type FrameKind = typ.Literal["text", "bytes", "json"]
type PayloadKind = typ.Literal["text", "bytes", "json", "close"]

_MISSING_WEBSOCKETS_MSG = (
    "WebSocketTestClient requires the 'websockets' package. Install "
    "falcon-pachinko[testing] to enable these helpers."
)
_EXPECTED_TEXT_MSG = "Expected text frame but received bytes"
_EXPECTED_BYTES_MSG = "Expected binary frame but received text"
_TEXT_PAYLOAD_REQUIRED_MSG = "Text frames require str payloads"
_BINARY_PAYLOAD_REQUIRED_MSG = "Binary frames require bytes payloads"
_UNSUPPORTED_FRAME_KIND_MSG = "Unsupported frame kind: {frame_kind}"
_FAILED_JSON_DECODE_MSG = (
    "Failed to decode JSON payload: frame_kind={kind}, length={length}, "
    "expected_type={expected}, exception={exception}"
)
_JSON_FRAME_REQUIRED_MSG = "JSON frames must be text or binary payloads"
_ORIGINAL_WS_RECEIVE_MSG = "Original websocket stub does not support receiving frames"
_INSECURE_WEBSOCKET_MSG = (
    "Insecure websocket URLs require allow_insecure=True. "
    "Use a wss:// URL for secure connections."
)


def _json_decode_message(raw: object, payload_type: type | None, exception: str) -> str:
    """Render only structural metadata for a failed JSON frame."""
    kind, length = _frame_metadata(raw)
    expected = _class_name(payload_type) if payload_type is not None else "unspecified"
    return _FAILED_JSON_DECODE_MSG.format(
        kind=kind,
        length=length,
        expected=expected,
        exception=exception,
    )


def _decode_json(
    decoder: msjson.Decoder,
    raw: object,
    payload_type: type | None,
) -> object:
    """Decode a test frame, raising a fresh vendor-class error without its text."""
    match raw:
        case str():
            data = raw.encode("utf-8")
        case bytes() | bytearray() | memoryview():
            data = bytes(raw)
        case _:
            details = (
                "Failed to decode JSON payload: "
                f"unsupported frame type={_type_name(raw)}"
            )
            raise TypeError(details)
    try:
        return decoder.decode(data)
    except ms.DecodeError as exc:
        error_type = (
            ms.ValidationError
            if isinstance(exc, ms.ValidationError)
            else ms.DecodeError
        )
        exception_name = _type_name(exc)
    # Suppressing traceback display alone leaves the vendor object reachable.
    # Raising after the except suite removes the implicit exception context too.
    raise error_type(_json_decode_message(raw, payload_type, exception_name))


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
        """``True`` once :meth:`accept` has been invoked."""
        return self._accepted

    @property
    def closed(self) -> bool:
        """``True`` once :meth:`close` has been invoked."""
        return self._closed

    @property
    def close_code(self) -> int | None:
        """Close code provided to :meth:`close`, if any."""
        return self._close_code

    @property
    def subprotocol(self) -> str | None:
        """Negotiated subprotocol, if any."""
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
