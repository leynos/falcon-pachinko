"""In-memory WebSocket simulators for hermetic testing."""

from __future__ import annotations

import asyncio
import typing as typ
from contextlib import asynccontextmanager

import msgspec.json as msjson

from ._common import (
    _BINARY_PAYLOAD_REQUIRED_MSG,
    _EXPECTED_BYTES_MSG,
    _EXPECTED_TEXT_MSG,
    _FAILED_JSON_DECODE_MSG,
    _TEXT_PAYLOAD_REQUIRED_MSG,
    _UNSUPPORTED_FRAME_KIND_MSG,
    FrameKind,
    _LifecycleSocket,
)


class WebSocketSimulator(_LifecycleSocket):
    """In-memory :class:`WebSocketLike` implementation for hermetic tests."""

    def __init__(
        self,
        *,
        inbound: asyncio.Queue[object] | None = None,
        outbound: asyncio.Queue[object] | None = None,
    ) -> None:
        super().__init__()
        self._inbound = inbound or asyncio.Queue()
        self._outbound = outbound or asyncio.Queue()
        self._json_encoder = msjson.Encoder()
        self._default_decoder = msjson.Decoder()
        self._decoders: dict[type[object], msjson.Decoder] = {}
        self.sent_messages: list[object] = []
        self.received_messages: list[object] = []

    @property
    def accepted(self) -> bool:
        """Return ``True`` once :meth:`accept` has been called."""
        return self._accepted

    @property
    def closed(self) -> bool:
        """Return ``True`` once :meth:`close` has been called."""
        return self._closed

    @property
    def close_code(self) -> int | None:
        """Return the code provided to :meth:`close`, if any."""
        return self._close_code

    @property
    def subprotocol(self) -> str | None:
        """Return the negotiated subprotocol, if any."""
        return self._subprotocol

    def pending_inbound(self) -> int:
        """Return the number of queued inbound frames."""
        return self._inbound.qsize()

    def pending_outbound(self) -> int:
        """Return the number of queued outbound frames."""
        return self._outbound.qsize()

    def _decoder_for(self, payload_type: type[object] | None) -> msjson.Decoder:
        """Return a cached decoder for ``payload_type``."""
        if payload_type is None:
            return self._default_decoder
        decoder = self._decoders.get(payload_type)
        if decoder is None:
            decoder = msjson.Decoder(payload_type)
            self._decoders[payload_type] = decoder
        return decoder

    async def accept(self, subprotocol: str | None = None) -> None:
        """Record that the handshake was accepted."""
        await super().accept(subprotocol=subprotocol)

    async def close(self, code: int = 1000) -> None:
        """Record that the connection was closed."""
        await super().close(code)

    async def send_media(self, data: object) -> None:
        """Record ``data`` as an outbound frame."""
        await self._outbound.put(data)
        self.sent_messages.append(data)

    async def receive_media(self) -> object:
        """Return the next inbound frame queued via :meth:`push_message`."""
        message = await self._inbound.get()
        self.received_messages.append(message)
        return message

    async def next_sent(self) -> object:
        """Await the next outbound frame emitted by the simulator."""
        return await self._outbound.get()

    def pop_sent(self) -> object:
        """Pop the next outbound frame synchronously."""
        try:
            return self._outbound.get_nowait()
        except asyncio.QueueEmpty as exc:
            msg = "No sent messages available"
            raise LookupError(msg) from exc

    async def send_text(self, message: str) -> None:
        """Send a UTF-8 text frame."""
        if not isinstance(message, str):
            raise TypeError(_TEXT_PAYLOAD_REQUIRED_MSG)
        await self.send_media(message)

    async def send_bytes(self, payload: bytes | bytearray | memoryview) -> None:
        """Send a binary frame."""
        if not isinstance(payload, bytes | bytearray | memoryview):
            raise TypeError(_BINARY_PAYLOAD_REQUIRED_MSG)
        await self.send_media(bytes(payload))

    async def send_json(self, payload: object) -> None:
        """Encode ``payload`` as JSON and send it as bytes."""
        await self.send_media(self._json_encoder.encode(payload))

    async def receive_text(self) -> str:
        """Receive the next frame ensuring it is textual."""
        message = await self.receive_media()
        if not isinstance(message, str):
            raise TypeError(_EXPECTED_TEXT_MSG)
        return message

    async def receive_bytes(self) -> bytes:
        """Receive the next frame ensuring it is binary."""
        message = await self.receive_media()
        if isinstance(message, bytes):
            return message
        raise TypeError(_EXPECTED_BYTES_MSG)

    async def receive_json(self, payload_type: type[object] | None = None) -> object:
        """Receive and decode a JSON payload."""
        message = await self.receive_media()
        if isinstance(message, str):
            data = message.encode("utf-8")
        elif isinstance(message, bytes | bytearray | memoryview):
            data = bytes(message)
        else:
            raise TypeError(_FAILED_JSON_DECODE_MSG.format(message=message))
        decoder = self._decoder_for(payload_type)
        return decoder.decode(data)

    async def push_message(self, payload: object, *, kind: FrameKind = "json") -> None:
        """Queue ``payload`` as if it were received from the peer."""
        data = self._prepare_inbound_payload(payload, kind)
        await self._inbound.put(data)

    def _prepare_inbound_payload(self, payload: object, kind: FrameKind) -> object:
        if kind == "text":
            return self._prepare_text_payload(payload)
        if kind == "bytes":
            return self._prepare_bytes_payload(payload)
        if kind == "json":
            return self._json_encoder.encode(payload)
        raise ValueError(
            _UNSUPPORTED_FRAME_KIND_MSG.format(frame_kind=kind)
        )  # pragma: no cover - safeguarded by FrameKind literal

    def _prepare_text_payload(self, payload: object) -> str:
        if not isinstance(payload, str):
            raise TypeError(_TEXT_PAYLOAD_REQUIRED_MSG)
        return payload

    def _prepare_bytes_payload(self, payload: object) -> bytes:
        if not isinstance(payload, bytes | bytearray | memoryview):
            raise TypeError(_BINARY_PAYLOAD_REQUIRED_MSG)
        return bytes(payload)

    async def push_text(self, message: str) -> None:
        """Queue a UTF-8 text frame."""
        await self.push_message(message, kind="text")

    async def push_bytes(self, payload: bytes | bytearray | memoryview) -> None:
        """Queue a binary frame."""
        await self.push_message(payload, kind="bytes")

    async def push_json(self, payload: object) -> None:
        """Queue a JSON payload."""
        await self.push_message(payload, kind="json")

    @asynccontextmanager
    async def connected(
        self, *, subprotocol: str | None = None
    ) -> typ.AsyncIterator[WebSocketSimulator]:
        """Accept the connection on entry and close it on exit."""
        await self.accept(subprotocol=subprotocol)
        try:
            yield self
        finally:
            if not self._closed:
                await self.close()
