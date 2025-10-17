"""Testing utilities for exercising websocket integrations."""

from __future__ import annotations

import dataclasses as dc
import typing as typ
from contextlib import asynccontextmanager
from urllib.parse import urlsplit

import msgspec.json as msjson

try:  # pragma: no cover - optional dependency exercised in tests
    from websockets.client import connect as _ws_connect
except ImportError:  # pragma: no cover - imported lazily in tests
    _ws_connect = None  # type: ignore[assignment]

if typ.TYPE_CHECKING:  # pragma: no cover - typing only
    from websockets.client import WebSocketClientProtocol
else:  # pragma: no cover - runtime fallback when dependency missing
    WebSocketClientProtocol = typ.Any  # type: ignore[misc,assignment]

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
_INSECURE_WEBSOCKET_MSG = (
    "Insecure websocket URLs require allow_insecure=True. "
    "Use a wss:// URL for secure connections."
)


class MissingDependencyError(RuntimeError):
    """Raised when optional testing dependencies are unavailable."""


@dc.dataclass(slots=True)
class TraceEvent:
    """Describe a frame exchanged during a traced websocket session."""

    direction: Direction
    kind: PayloadKind
    payload: object


class WebSocketSession:
    """Facade around a websocket client connection with helpful utilities."""

    def __init__(
        self,
        connection: WebSocketClientProtocol,
        *,
        path: str,
        trace: list[TraceEvent] | None,
    ) -> None:
        self._connection = connection
        self.path = path
        self.trace = trace
        self._json_encoder = msjson.Encoder()
        self._default_decoder = msjson.Decoder()
        self._decoders: dict[type[object], msjson.Decoder] = {}

    @property
    def subprotocol(self) -> str | None:
        """Return the negotiated subprotocol, if any."""
        return getattr(self._connection, "subprotocol", None)

    @property
    def closed(self) -> bool:
        """Whether the underlying websocket has been closed."""
        return bool(getattr(self._connection, "closed", False))

    def _log(self, direction: Direction, kind: PayloadKind, payload: object) -> None:
        """Append a trace event if tracing is enabled."""
        if self.trace is not None:
            self.trace.append(
                TraceEvent(direction=direction, kind=kind, payload=payload)
            )

    def _encode_json(self, payload: object) -> str:
        """Encode ``payload`` as UTF-8 JSON text."""
        data = self._json_encoder.encode(payload)
        return data.decode("utf-8")

    async def send(
        self, payload: str | bytes | object, *, kind: FrameKind | None = None
    ) -> None:
        """Send a frame, inferring the payload kind when omitted."""
        frame_kind = self._determine_send_frame_kind(kind, payload)
        data = self._encode_payload(frame_kind, payload)
        await self._connection.send(data)
        self._log("send", frame_kind, payload)

    def _determine_send_frame_kind(
        self, kind: FrameKind | None, payload: str | bytes | object
    ) -> FrameKind:
        """Return the frame kind inferred from ``kind`` or ``payload``."""
        if kind is not None:
            return kind
        if isinstance(payload, bytes):
            return "bytes"
        if isinstance(payload, str):
            return "text"
        return "json"

    def _encode_payload(
        self, frame_kind: FrameKind, payload: str | bytes | object
    ) -> str | bytes:
        """Encode ``payload`` according to ``frame_kind``."""
        if frame_kind == "text":
            return self._encode_text_payload(payload)
        if frame_kind == "bytes":
            return self._encode_bytes_payload(payload)
        if frame_kind == "json":
            return self._encode_json(payload)
        raise ValueError(  # pragma: no cover - safeguarded by the FrameKind literal
            _UNSUPPORTED_FRAME_KIND_MSG.format(frame_kind=frame_kind)
        )

    def _encode_text_payload(self, payload: str | bytes | object) -> str:
        """Validate and return a text payload."""
        if isinstance(payload, str):
            return payload
        raise TypeError(_TEXT_PAYLOAD_REQUIRED_MSG)

    def _encode_bytes_payload(self, payload: str | bytes | object) -> bytes:
        """Validate and return a binary payload."""
        if isinstance(payload, bytes):
            return payload
        raise TypeError(_BINARY_PAYLOAD_REQUIRED_MSG)

    async def send_text(self, message: str) -> None:
        """Send a text frame."""
        await self.send(message, kind="text")

    async def send_bytes(self, payload: bytes) -> None:
        """Send a binary frame."""
        await self.send(payload, kind="bytes")

    async def send_json(self, payload: object) -> None:
        """Send a JSON payload using msgspec for encoding."""
        await self.send(payload, kind="json")

    async def _recv_raw(self) -> str | bytes:
        """Receive the next frame without decoding."""
        return await self._connection.recv()

    def _decoder_for(self, payload_type: type[object] | None) -> msjson.Decoder:
        """Return a JSON decoder for the requested payload type."""
        if payload_type is None:
            return self._default_decoder
        decoder = self._decoders.get(payload_type)
        if decoder is None:
            decoder = msjson.Decoder(payload_type)
            self._decoders[payload_type] = decoder
        return decoder

    async def receive(
        self,
        *,
        kind: FrameKind | None = None,
        payload_type: type[object] | None = None,
    ) -> object:
        """Receive a frame and decode it according to ``kind``."""
        message = await self._recv_raw()
        frame_kind = self._determine_frame_kind(kind, message)
        payload = self._decode_frame(frame_kind, message, payload_type)
        self._log("receive", frame_kind, payload)
        return payload

    def _determine_frame_kind(
        self, kind: FrameKind | None, message: str | bytes
    ) -> FrameKind:
        """Return the frame kind inferred from ``kind`` or ``message``."""
        if kind is not None:
            return kind
        return "text" if isinstance(message, str) else "bytes"

    def _decode_frame(
        self,
        frame_kind: FrameKind,
        message: str | bytes,
        payload_type: type[object] | None,
    ) -> object:
        """Decode ``message`` according to ``frame_kind``."""
        if frame_kind == "json":
            return self._decode_json_frame(message, payload_type)
        if frame_kind == "text":
            return self._decode_text_frame(message)
        if frame_kind == "bytes":
            return self._decode_bytes_frame(message)
        raise ValueError(  # pragma: no cover - safeguarded by the FrameKind literal
            _UNSUPPORTED_FRAME_KIND_MSG.format(frame_kind=frame_kind)
        )

    def _decode_json_frame(
        self, message: str | bytes, payload_type: type[object] | None
    ) -> object:
        """Decode ``message`` as JSON using ``payload_type`` when provided."""
        data = message.encode("utf-8") if isinstance(message, str) else message
        decoder = self._decoder_for(payload_type)
        try:
            return decoder.decode(data)
        except Exception as exc:  # pragma: no cover - msgspec raised
            raise RuntimeError(_FAILED_JSON_DECODE_MSG.format(message=message)) from exc

    def _decode_text_frame(self, message: str | bytes) -> str:
        """Validate and return a text frame payload."""
        if isinstance(message, str):
            return message
        raise TypeError(_EXPECTED_TEXT_MSG)

    def _decode_bytes_frame(self, message: str | bytes) -> bytes:
        """Validate and return a binary frame payload."""
        if isinstance(message, bytes):
            return message
        raise TypeError(_EXPECTED_BYTES_MSG)

    async def receive_text(self) -> str:
        """Receive a text frame."""
        message = await self.receive(kind="text")
        if not isinstance(message, str):  # pragma: no cover - safeguarded upstream
            raise TypeError(_EXPECTED_TEXT_MSG)
        return message

    async def receive_bytes(self) -> bytes:
        """Receive a binary frame."""
        message = await self.receive(kind="bytes")
        if not isinstance(message, bytes):  # pragma: no cover - safeguarded upstream
            raise TypeError(_EXPECTED_BYTES_MSG)
        return message

    async def receive_json(self, payload_type: type[object] | None = None) -> object:
        """Receive and decode a JSON payload."""
        return await self.receive(kind="json", payload_type=payload_type)

    async def close(self, code: int = 1000, reason: str = "") -> None:
        """Close the websocket connection."""
        try:
            await self._connection.close(code=code, reason=reason)
        except Exception as exc:  # pragma: no cover - close failures are rare
            self._log(
                "error",
                "close",
                {"code": code, "reason": reason, "exception": str(exc)},
            )
            raise
        else:
            self._log("close", "close", {"code": code, "reason": reason})


class _ClientOptions(typ.TypedDict, total=False):
    """Optional configuration parameters for :class:`WebSocketTestClient`."""

    default_headers: typ.Mapping[str, str]
    subprotocols: typ.Sequence[str]
    open_timeout: float
    capture_trace: bool
    trace_factory: typ.Callable[[], list[TraceEvent]]
    allow_insecure: bool


class WebSocketTestClient:
    """High-level client tailored for websocket integration tests."""

    def __init__(
        self,
        base_url: str,
        **options: typ.Unpack[_ClientOptions],
    ) -> None:
        """Configure the client with optional keyword-only ``options``.

        Accepted keys are:

        - ``default_headers``: base headers merged into each connection.
        - ``subprotocols``: preferred subprotocols offered on connect.
        - ``open_timeout``: connection timeout in seconds (default ``10.0``).
        - ``capture_trace``: capture trace events by default (default ``False``).
        - ``trace_factory``: callable returning a new trace list (default ``list``).
        - ``allow_insecure``: allow ``ws://`` URLs (default ``False``).
        """
        default_headers = options.get("default_headers")
        subprotocols = options.get("subprotocols")
        open_timeout = options.get("open_timeout", 10.0)
        capture_trace = options.get("capture_trace", False)
        trace_factory = options.get("trace_factory")
        allow_insecure = options.get("allow_insecure", False)

        self._base_url = base_url.rstrip("/") or base_url
        self._default_headers = dict(default_headers or {})
        self._subprotocols = tuple(subprotocols) if subprotocols is not None else None
        self._open_timeout = open_timeout
        self._capture_trace = capture_trace
        self._trace_factory = trace_factory or list
        self._allow_insecure = allow_insecure

        parsed_base = urlsplit(self._base_url)
        if parsed_base.scheme == "ws" and not self._allow_insecure:
            raise ValueError(_INSECURE_WEBSOCKET_MSG)

    def _build_url(self, path: str) -> tuple[str, str]:
        """Return the absolute connection URL and normalized path."""
        parsed = urlsplit(path)
        if parsed.scheme in {"ws", "wss"}:
            if parsed.scheme == "ws" and not self._allow_insecure:
                raise ValueError(_INSECURE_WEBSOCKET_MSG)
            normalized = parsed.path or "/"
            if parsed.query:
                normalized = f"{normalized}?{parsed.query}"
            return path, normalized
        normalized = parsed.path or path
        if parsed.query:
            normalized = f"{normalized}?{parsed.query}"
        if not normalized.startswith("/"):
            normalized = f"/{normalized}"
        base = self._base_url.rstrip("/")
        return f"{base}{normalized}", normalized

    def _merge_headers(
        self, headers: typ.Mapping[str, str] | None
    ) -> dict[str, str] | None:
        """Merge default headers with per-connection overrides."""
        if not self._default_headers and not headers:
            return None
        merged = dict(self._default_headers)
        if headers:
            merged |= headers
        return merged

    def _should_create_new_trace_list(
        self, *, trace: list[TraceEvent] | bool | None
    ) -> bool:
        """Determine whether to create a new trace list for the session.

        Returns True when:
        - trace is explicitly True (caller requests tracing), or
        - trace is None (use default) and instance capture_trace is enabled
        """
        explicit_enable = trace is True
        use_instance_default = trace is None and self._capture_trace
        return explicit_enable or use_instance_default

    @asynccontextmanager
    async def connect(
        self,
        path: str,
        *,
        headers: typ.Mapping[str, str] | None = None,
        subprotocols: typ.Sequence[str] | None = None,
        trace: list[TraceEvent] | bool | None = None,
    ) -> typ.AsyncIterator[WebSocketSession]:
        """Connect to ``path`` and yield a managed :class:`WebSocketSession`.

        ``trace`` accepts one of four values:

        - ``list``: use the provided list to record trace events.
        - ``True``: create and return a new trace list via ``trace_factory``.
        - ``False``: disable tracing for this session.
        - ``None``: fall back to the client's ``capture_trace`` default.
        """
        global _ws_connect
        ws_connect = _ws_connect
        if ws_connect is None:  # pragma: no cover - exercised via import error test
            try:
                from websockets.client import connect as ws_connect  # type: ignore
            except ImportError as exc:  # pragma: no cover - optional dependency
                raise MissingDependencyError(_MISSING_WEBSOCKETS_MSG) from exc
            _ws_connect = ws_connect
        url, normalized_path = self._build_url(path)
        negotiated = (
            tuple(subprotocols) if subprotocols is not None else self._subprotocols
        )
        if isinstance(trace, list):
            trace_log = trace
        elif self._should_create_new_trace_list(trace=trace):
            trace_log = self._trace_factory()
        else:
            trace_log = None
        async with ws_connect(
            url,
            extra_headers=self._merge_headers(headers),
            subprotocols=negotiated,
            open_timeout=self._open_timeout,
        ) as connection:
            session = WebSocketSession(
                connection,
                path=normalized_path,
                trace=trace_log,
            )
            try:
                yield session
            finally:
                if not session.closed:
                    await session.close()


__all__ = ["MissingDependencyError", "TraceEvent", "WebSocketTestClient"]
