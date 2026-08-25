"""High-level WebSocket test client for integration testing."""

from __future__ import annotations

import dataclasses as dc
import typing as typ
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from urllib.parse import urlsplit

import msgspec.json as msjson

from ._common import (
    _BINARY_PAYLOAD_REQUIRED_MSG,
    _EXPECTED_BYTES_MSG,
    _EXPECTED_TEXT_MSG,
    _FAILED_JSON_DECODE_MSG,
    _INSECURE_WEBSOCKET_MSG,
    _MISSING_WEBSOCKETS_MSG,
    _TEXT_PAYLOAD_REQUIRED_MSG,
    _UNSUPPORTED_FRAME_KIND_MSG,
    Direction,
    FrameKind,
    MissingDependencyError,
    PayloadKind,
)

try:  # pragma: no cover - optional dependency exercised in tests
    from websockets.client import connect as _ws_connect
except ImportError:  # pragma: no cover - imported lazily in tests
    _ws_connect = None

if typ.TYPE_CHECKING:  # pragma: no cover - typing only
    import collections.abc as cabc
    from urllib.parse import SplitResult

    from websockets.client import WebSocketClientProtocol


@dc.dataclass(slots=True)
class TraceEvent:
    """Describe a frame exchanged during a traced websocket session."""

    index: int
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
        self._next_trace_index = 0

    @property
    def subprotocol(self) -> str | None:
        """Negotiated subprotocol, if any."""
        return getattr(self._connection, "subprotocol", None)

    @property
    def closed(self) -> bool:
        """Whether the underlying websocket has been closed."""
        return bool(getattr(self._connection, "closed", False))

    def _log(self, direction: Direction, kind: PayloadKind, payload: object) -> None:
        """Append a trace event if tracing is enabled."""
        if self.trace is not None:
            event = TraceEvent(
                index=self._next_trace_index,
                direction=direction,
                kind=kind,
                payload=payload,
            )
            self.trace.append(event)
            self._next_trace_index += 1

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

    @staticmethod
    def _determine_send_frame_kind(
        kind: FrameKind | None, payload: str | bytes | object
    ) -> FrameKind:
        """Return the frame kind inferred from ``kind`` or ``payload``."""
        if kind is not None:
            return kind
        match payload:
            case bytes():
                return "bytes"
            case str():
                return "text"
            case _:
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

    @staticmethod
    def _encode_text_payload(payload: str | bytes | object) -> str:
        """Validate and return a text payload."""
        if isinstance(payload, str):
            return payload
        raise TypeError(_TEXT_PAYLOAD_REQUIRED_MSG)

    @staticmethod
    def _encode_bytes_payload(payload: str | bytes | object) -> bytes:
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

    @staticmethod
    def _determine_frame_kind(
        kind: FrameKind | None, message: str | bytes
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

    @staticmethod
    def _decode_text_frame(message: str | bytes) -> str:
        """Validate and return a text frame payload."""
        if isinstance(message, str):
            return message
        raise TypeError(_EXPECTED_TEXT_MSG)

    @staticmethod
    def _decode_bytes_frame(message: str | bytes) -> bytes:
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
        self._log("close", "close", {"code": code, "reason": reason})


class _ClientOptions(typ.TypedDict, total=False):
    """Optional configuration parameters for :class:`WebSocketTestClient`."""

    default_headers: cabc.Mapping[str, str]
    subprotocols: cabc.Sequence[str]
    open_timeout: float
    capture_trace: bool
    trace_factory: cabc.Callable[[], list[TraceEvent]]
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

        Raises
        ------
        ValueError
            If ``base_url`` uses the insecure ``ws://`` scheme and
            ``allow_insecure`` is not enabled.
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
            return self._handle_absolute_url(path, parsed)
        return self._handle_relative_url(path, parsed)

    def _handle_absolute_url(self, path: str, parsed: SplitResult) -> tuple[str, str]:
        """Handle an absolute WebSocket URL with validation.

        Returns
        -------
        tuple[str, str]
            The unchanged connection URL and the normalized request path.

        Raises
        ------
        ValueError
            If ``parsed`` uses the insecure ``ws://`` scheme and the client was
            not configured with ``allow_insecure``.
        """
        if parsed.scheme == "ws" and not self._allow_insecure:
            raise ValueError(_INSECURE_WEBSOCKET_MSG)
        normalized = self._append_query(parsed.path or "/", parsed.query)
        return path, normalized

    def _handle_relative_url(self, path: str, parsed: SplitResult) -> tuple[str, str]:
        """Handle a relative path by joining it with the base URL."""
        normalized = parsed.path or path
        normalized = self._append_query(normalized, parsed.query)
        if not normalized.startswith("/"):
            normalized = f"/{normalized}"
        base = self._base_url.rstrip("/")
        return f"{base}{normalized}", normalized

    @staticmethod
    def _append_query(path: str, query: str) -> str:
        """Append query string to path if present."""
        if query:
            return f"{path}?{query}"
        return path

    def _merge_headers(
        self, headers: cabc.Mapping[str, str] | None
    ) -> dict[str, str] | None:
        """Merge default headers with per-connection overrides."""
        if not self._default_headers and not headers:
            return None
        merged = dict(self._default_headers)
        if headers:
            merged |= headers
        return merged

    def _resolve_subprotocols(
        self, subprotocols: cabc.Sequence[str] | None
    ) -> tuple[str, ...] | None:
        """Return connection subprotocols, honouring per-call overrides."""
        if subprotocols is not None:
            return tuple(subprotocols)
        return self._subprotocols

    def _should_create_new_trace_list(
        self, *, trace: list[TraceEvent] | bool | None
    ) -> bool:
        """Determine whether to create a new trace list for the session.

        Returns
        -------
        bool
            ``True`` when ``trace`` is explicitly ``True`` (the caller requests
            tracing), or when ``trace`` is ``None`` and the client's
            ``capture_trace`` default is enabled; ``False`` otherwise.
        """
        explicit_enable = trace is True
        use_instance_default = trace is None and self._capture_trace
        return explicit_enable or use_instance_default

    @staticmethod
    def _ensure_ws_connect() -> cabc.Callable[
        ..., cabc.Awaitable[WebSocketClientProtocol]
    ]:
        """Return the websockets connect callable, importing lazily when needed."""
        global _ws_connect
        ws_connect = _ws_connect
        if ws_connect is None:  # pragma: no cover - exercised via import error test
            try:
                from websockets.client import connect as ws_connect
            except ImportError as exc:  # pragma: no cover - optional dependency
                raise MissingDependencyError(_MISSING_WEBSOCKETS_MSG) from exc
            _ws_connect = ws_connect
        return ws_connect

    def _prepare_connection_params(
        self,
        path: str,
        headers: cabc.Mapping[str, str] | None,
        subprotocols: cabc.Sequence[str] | None,
    ) -> tuple[str, str, dict[str, str] | None, tuple[str, ...] | None]:
        """Compute the URL, normalized path, headers, and subprotocols."""
        url, normalized_path = self._build_url(path)
        merged_headers = self._merge_headers(headers)
        negotiated = self._resolve_subprotocols(subprotocols)
        return url, normalized_path, merged_headers, negotiated

    def _configure_trace(
        self, *, trace: list[TraceEvent] | bool | None
    ) -> list[TraceEvent] | None:
        """Resolve the trace log to use for the connection session."""
        if isinstance(trace, list):
            return trace
        if self._should_create_new_trace_list(trace=trace):
            return self._trace_factory()
        return None

    @asynccontextmanager
    async def connect(
        self,
        path: str,
        *,
        headers: cabc.Mapping[str, str] | None = None,
        subprotocols: cabc.Sequence[str] | None = None,
        trace: list[TraceEvent] | bool | None = None,
    ) -> cabc.AsyncIterator[WebSocketSession]:
        """Connect to ``path`` and yield a managed :class:`WebSocketSession`.

        ``trace`` accepts one of four values:

        - ``list``: use the provided list to record trace events.
        - ``True``: create and return a new trace list via ``trace_factory``.
        - ``False``: disable tracing for this session.
        - ``None``: fall back to the client's ``capture_trace`` default.

        Yields
        ------
        WebSocketSession
            A session bound to the open connection. The session is closed on
            exit if the caller has not already closed it.
        """
        ws_connect = self._ensure_ws_connect()
        (
            url,
            normalized_path,
            merged_headers,
            negotiated,
        ) = self._prepare_connection_params(path, headers, subprotocols)
        trace_log = self._configure_trace(trace=trace)
        connect_cm = typ.cast(
            "AbstractAsyncContextManager[WebSocketClientProtocol]",
            ws_connect(
                url,
                extra_headers=merged_headers,
                subprotocols=negotiated,
                open_timeout=self._open_timeout,
            ),
        )
        async with connect_cm as connection:
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
