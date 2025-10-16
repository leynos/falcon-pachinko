"""Testing utilities for exercising websocket integrations."""

from __future__ import annotations

import dataclasses as dc
import typing as typ
from contextlib import asynccontextmanager
from urllib.parse import urlsplit

import msgspec.json as msjson

Direction = typ.Literal["send", "receive", "close"]
PayloadKind = typ.Literal["text", "bytes", "json", "close"]

_MISSING_WEBSOCKETS_MSG = (
    "WebSocketTestClient requires the 'websockets' package. Install "
    "falcon-pachinko[testing] to enable these helpers."
)
_EXPECTED_TEXT_MSG = "Expected text frame but received bytes"
_EXPECTED_BYTES_MSG = "Expected binary frame but received text"
_UNEXPECTED_FRAME_MSG = "Unexpected frame type received"

if typ.TYPE_CHECKING:  # pragma: no cover - imported for typing only
    import websockets.client as ws_client
    from websockets.client import WebSocketClientProtocol
else:  # pragma: no cover - runtime fallback
    ws_client = None  # type: ignore[assignment]
    WebSocketClientProtocol = typ.Any  # type: ignore[misc,assignment]


class MissingDependencyError(RuntimeError):
    """Raised when optional testing dependencies are unavailable."""


class _ClientModule(typ.Protocol):
    """Subset of the ``websockets.client`` module used by the test client."""

    def connect(
        self,
        uri: str,
        *,
        extra_headers: typ.Mapping[str, str] | None = ...,  # pragma: no cover - stub
        subprotocols: typ.Sequence[str] | None = ...,  # pragma: no cover - stub
        open_timeout: float | None = ...,  # pragma: no cover - stub
    ) -> typ.AsyncContextManager[WebSocketClientProtocol]:  # pragma: no cover - stub
        ...


def _require_websockets() -> _ClientModule:
    """Return the ``websockets.client`` module or raise a helpful error."""
    global ws_client
    if ws_client is None:  # pragma: no cover - exercised via import error tests
        try:
            import websockets.client as ws_client  # type: ignore[assignment]
        except ImportError as exc:  # pragma: no cover - environment specific
            raise MissingDependencyError(_MISSING_WEBSOCKETS_MSG) from exc
    return typ.cast("_ClientModule", ws_client)


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

    async def send_text(self, message: str) -> None:
        """Send a text frame."""
        await self._connection.send(message)
        self._log("send", "text", message)

    async def send_bytes(self, payload: bytes) -> None:
        """Send a binary frame."""
        await self._connection.send(payload)
        self._log("send", "bytes", payload)

    def _encode_json(self, payload: object) -> str:
        """Encode ``payload`` as UTF-8 JSON text."""
        data = self._json_encoder.encode(payload)
        return data.decode("utf-8")

    async def send_json(self, payload: object) -> None:
        """Send a JSON payload using msgspec for encoding."""
        message = self._encode_json(payload)
        await self._connection.send(message)
        self._log("send", "json", payload)

    async def _recv_raw(self) -> str | bytes:
        """Receive the next frame without decoding."""
        return await self._connection.recv()

    async def receive(self) -> str | bytes:
        """Receive a frame without interpretation."""
        message = await self._recv_raw()
        kind: PayloadKind = "text" if isinstance(message, str) else "bytes"
        self._log("receive", kind, message)
        return message

    async def receive_text(self) -> str:
        """Receive a text frame."""
        message = await self.receive()
        if isinstance(message, str):
            return message
        raise TypeError(_EXPECTED_TEXT_MSG)

    async def receive_bytes(self) -> bytes:
        """Receive a binary frame."""
        message = await self.receive()
        if isinstance(message, bytes):
            return message
        raise TypeError(_EXPECTED_BYTES_MSG)

    def _decoder_for(self, payload_type: type[object] | None) -> msjson.Decoder:
        """Return a JSON decoder for the requested payload type."""
        if payload_type is None:
            return self._default_decoder
        decoder = self._decoders.get(payload_type)
        if decoder is None:
            decoder = msjson.Decoder(payload_type)
            self._decoders[payload_type] = decoder
        return decoder

    async def receive_json(self, payload_type: type[object] | None = None) -> object:
        """Receive and decode a JSON payload."""
        message = await self._recv_raw()
        if isinstance(message, str):
            data = message.encode("utf-8")
        elif isinstance(message, bytes):
            data = message
        else:  # pragma: no cover - websockets always returns str | bytes
            raise TypeError(_UNEXPECTED_FRAME_MSG)
        decoder = self._decoder_for(payload_type)
        payload = decoder.decode(data)
        self._log("receive", "json", payload)
        return payload

    async def close(self, code: int = 1000, reason: str = "") -> None:
        """Close the websocket connection."""
        await self._connection.close(code=code, reason=reason)
        self._log("close", "close", {"code": code, "reason": reason})


class WebSocketTestClient:
    """High-level client tailored for websocket integration tests."""

    def __init__(
        self,
        base_url: str,
        *,
        default_headers: typ.Mapping[str, str] | None = None,
        subprotocols: typ.Sequence[str] | None = None,
        open_timeout: float | None = 10.0,
        capture_trace: bool = False,
        trace_factory: typ.Callable[[], list[TraceEvent]] | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/") or base_url
        self._default_headers = dict(default_headers or {})
        self._subprotocols = tuple(subprotocols) if subprotocols is not None else None
        self._open_timeout = open_timeout
        self._capture_trace = capture_trace
        self._trace_factory = trace_factory or list

    def _build_url(self, path: str) -> tuple[str, str]:
        """Return the absolute connection URL and normalized path."""
        if path.startswith(("ws://", "wss://")):
            parsed = urlsplit(path)
            normalized = parsed.path or "/"
            if parsed.query:
                normalized = f"{normalized}?{parsed.query}"
            return path, normalized
        if not path.startswith("/"):
            path = f"/{path}"
        if self._base_url.endswith("/"):
            return f"{self._base_url.rstrip('/')}{path}", path
        return f"{self._base_url}{path}", path

    def _merge_headers(
        self, headers: typ.Mapping[str, str] | None
    ) -> dict[str, str] | None:
        """Merge default headers with per-connection overrides."""
        if not self._default_headers and not headers:
            return None
        merged = dict(self._default_headers)
        if headers:
            merged.update(headers)
        return merged

    def _should_trace(
        self, *, capture_trace: bool | None, trace: list[TraceEvent] | None
    ) -> bool:
        """Return whether tracing should be enabled for this session."""
        if trace is not None:
            return True
        if capture_trace is None:
            return self._capture_trace
        return capture_trace

    @asynccontextmanager
    async def connect(
        self,
        path: str,
        *,
        headers: typ.Mapping[str, str] | None = None,
        subprotocols: typ.Sequence[str] | None = None,
        trace: list[TraceEvent] | None = None,
        capture_trace: bool | None = None,
    ) -> typ.AsyncIterator[WebSocketSession]:
        """Connect to ``path`` and yield a managed :class:`WebSocketSession`."""
        module = _require_websockets()
        url, normalized_path = self._build_url(path)
        negotiated = (
            tuple(subprotocols) if subprotocols is not None else self._subprotocols
        )
        trace_log = trace
        if self._should_trace(capture_trace=capture_trace, trace=trace_log):
            trace_log = trace_log or self._trace_factory()
        async with module.connect(
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
