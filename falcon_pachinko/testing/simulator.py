"""In-memory WebSocket simulators for hermetic testing."""

from __future__ import annotations

import asyncio
import dataclasses as dc
import typing as typ
from contextlib import asynccontextmanager

import falcon
import msgspec.json as msjson

from ._common import (
    _BINARY_PAYLOAD_REQUIRED_MSG,
    _EXPECTED_BYTES_MSG,
    _EXPECTED_TEXT_MSG,
    _TEXT_PAYLOAD_REQUIRED_MSG,
    _UNSUPPORTED_FRAME_KIND_MSG,
    FrameKind,
    _decode_json,
    _LifecycleSocket,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc


@dc.dataclass(frozen=True, slots=True)
class _DisconnectMarker:
    """Signal a peer disconnect through the simulator's inbound queue."""

    code: int


# The simulator API exposes focused queue and lifecycle controls.
class WebSocketSimulator(_LifecycleSocket):  # ruff: ignore[too-many-public-methods]
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
        self.lifecycle_event = asyncio.Event()
        self._disconnect_code: int | None = None
        self._receive_waiting = False
        self._json_encoder = msjson.Encoder()
        self._default_decoder = msjson.Decoder()
        self._decoders: dict[type[object], msjson.Decoder] = {}
        self.sent_messages: list[object] = []
        self.received_messages: list[object] = []

    # pylint: disable-next=trivial-attribute-wrapper  # deliberate public API: the inbound queue is private, so callers need this accessor to inspect backlog depth
    def pending_inbound(self) -> int:
        """Return the number of queued inbound frames."""
        return self._inbound.qsize()

    # pylint: disable-next=trivial-attribute-wrapper  # deliberate public API: the outbound queue is private, so callers need this accessor to inspect backlog depth
    def pending_outbound(self) -> int:
        """Return the number of queued outbound frames."""
        return self._outbound.qsize()

    def _decoder_for(self, payload_type: type[object] | None) -> msjson.Decoder:
        """Return a cached decoder for ``payload_type``."""
        if payload_type is None:
            return self._default_decoder
        if (decoder := self._decoders.get(payload_type)) is None:
            decoder = self._decoders[payload_type] = msjson.Decoder(payload_type)
        return decoder

    async def send_media(self, data: object) -> None:
        """Record ``data`` as an outbound frame."""
        await self._outbound.put(data)
        self.sent_messages.append(data)

    async def receive_media(self) -> object:
        """Return the next inbound frame queued via :meth:`push_message`."""
        if self._disconnect_code is not None:
            raise falcon.WebSocketDisconnected(code=self._disconnect_code)
        if self.closed:
            self._disconnect_code = self.close_code or 1000
            raise falcon.WebSocketDisconnected(code=self._disconnect_code)
        self._receive_waiting = True
        try:
            message = await self._inbound.get()
        finally:
            self._receive_waiting = False
        if isinstance(message, _DisconnectMarker):
            self._disconnect_code = message.code
            await self.close(code=message.code)
            raise falcon.WebSocketDisconnected(code=message.code)
        self.received_messages.append(message)
        return message

    @typ.override
    async def accept(self, subprotocol: str | None = None) -> None:
        """Accept the simulated peer and wake lifecycle waiters."""
        await super().accept(subprotocol=subprotocol)
        self.lifecycle_event.set()

    @typ.override
    async def close(self, code: int = 1000) -> None:
        """Close the simulated peer and wake receive and lifecycle waiters."""
        should_signal_receiver = self._disconnect_code is None
        if should_signal_receiver:
            self._disconnect_code = code
            if self._receive_waiting:
                await self._inbound.put(_DisconnectMarker(code))
        await super().close(code=code)
        self.lifecycle_event.set()

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
        """Receive and decode a JSON payload.

        Returns
        -------
        object
            The decoded payload, using ``payload_type`` when supplied.

        Unsupported frames raise ``TypeError`` through the shared decoder.
        """
        message = await self.receive_media()
        return _decode_json(self._decoder_for(payload_type), message, payload_type)

    async def push_message(self, payload: object, *, kind: FrameKind = "json") -> None:
        """Queue ``payload`` as if it were received from the peer."""
        data = self._prepare_inbound_payload(payload, kind)
        await self._inbound.put(data)

    async def push_disconnect(self, code: int = 1000) -> None:
        """Queue a peer disconnect after any frames already in the queue."""
        await self._inbound.put(_DisconnectMarker(code))

    def _prepare_inbound_payload(self, payload: object, kind: FrameKind) -> object:
        match kind:
            case "text":
                return self._prepare_text_payload(payload)
            case "bytes":
                return self._prepare_bytes_payload(payload)
            case "json":
                return self._json_encoder.encode(payload)
            case _:  # pragma: no cover - safeguarded by FrameKind literal
                raise ValueError(_UNSUPPORTED_FRAME_KIND_MSG.format(frame_kind=kind))

    @staticmethod
    def _prepare_text_payload(payload: object) -> str:
        if not isinstance(payload, str):
            raise TypeError(_TEXT_PAYLOAD_REQUIRED_MSG)
        return payload

    @staticmethod
    def _prepare_bytes_payload(payload: object) -> bytes:
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
    ) -> cabc.AsyncIterator[WebSocketSimulator]:
        """Accept the connection on entry and close it on exit."""
        await self.accept(subprotocol=subprotocol)
        try:
            yield self
        finally:
            if not self._closed:
                await self.close()
