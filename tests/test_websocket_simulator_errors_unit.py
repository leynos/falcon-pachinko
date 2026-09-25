"""Tests for error paths and frame-kind handling in WebSocketSimulator."""

from __future__ import annotations

import asyncio
import typing as typ

import pytest

from falcon_pachinko.testing import WebSocketSimulator


def test_pop_sent_raises_lookup_error_when_empty() -> None:
    """Popping from an empty outbound queue raises LookupError."""
    simulator = WebSocketSimulator()

    with pytest.raises(LookupError, match="No sent messages available"):
        simulator.pop_sent()


@pytest.mark.asyncio
async def test_send_text_rejects_non_str_payload() -> None:
    """Sending a non-string payload as text raises TypeError."""
    simulator = WebSocketSimulator()
    not_text = typ.cast("str", b"not text")

    with pytest.raises(TypeError, match="Text frames require str payloads"):
        await simulator.send_text(not_text)


@pytest.mark.asyncio
async def test_send_bytes_rejects_non_bytes_payload() -> None:
    """Sending a non-bytes payload as binary raises TypeError."""
    simulator = WebSocketSimulator()
    not_bytes = typ.cast("bytes", "not bytes")

    with pytest.raises(TypeError, match="Binary frames require bytes payloads"):
        await simulator.send_bytes(not_bytes)


@pytest.mark.asyncio
async def test_send_bytes_accepts_bytearray_and_sends_bytes() -> None:
    """A bytearray payload is accepted and recorded as bytes."""
    simulator = WebSocketSimulator()

    await simulator.send_bytes(bytearray(b"abc"))
    sent = simulator.pop_sent()

    assert sent == b"abc", "the bytearray payload must be sent as bytes"
    assert isinstance(sent, bytes), "the sent frame must be converted to bytes"


@pytest.mark.asyncio
async def test_send_bytes_accepts_memoryview_and_sends_bytes() -> None:
    """A memoryview payload is accepted and recorded as bytes."""
    simulator = WebSocketSimulator()

    await simulator.send_bytes(memoryview(b"xyz"))
    sent = simulator.pop_sent()

    assert sent == b"xyz", "the memoryview payload must be sent as bytes"
    assert isinstance(sent, bytes), "the sent frame must be converted to bytes"


@pytest.mark.asyncio
async def test_receive_text_rejects_non_text_frame() -> None:
    """Receiving a binary frame via receive_text raises TypeError."""
    simulator = WebSocketSimulator()
    await simulator.push_bytes(b"payload")

    with pytest.raises(TypeError, match="Expected text frame but received bytes"):
        await simulator.receive_text()


@pytest.mark.asyncio
async def test_receive_bytes_returns_bytes_frame() -> None:
    """Receiving a bytes frame via receive_bytes returns the raw bytes."""
    simulator = WebSocketSimulator()
    await simulator.push_bytes(b"payload")

    received = await simulator.receive_bytes()

    assert received == b"payload", "receive_bytes must return the queued bytes"


@pytest.mark.asyncio
async def test_receive_bytes_rejects_non_bytes_frame() -> None:
    """Receiving a text frame via receive_bytes raises TypeError."""
    simulator = WebSocketSimulator()
    await simulator.push_text("payload")

    with pytest.raises(TypeError, match="Expected binary frame but received text"):
        await simulator.receive_bytes()


@pytest.mark.asyncio
async def test_receive_json_decodes_text_frame() -> None:
    """Receiving JSON queued as a text frame decodes correctly."""
    simulator = WebSocketSimulator()
    await simulator.push_message('{"hello": "world"}', kind="text")

    payload = await simulator.receive_json()

    assert payload == {"hello": "world"}, "the text frame must decode as JSON"


@pytest.mark.asyncio
async def test_receive_json_rejects_unsupported_frame_type() -> None:
    """Receiving a non-str, non-bytes frame via receive_json raises TypeError."""
    inbound: asyncio.Queue[object] = asyncio.Queue()
    simulator = WebSocketSimulator(inbound=inbound)
    await inbound.put(123)

    with pytest.raises(TypeError, match="Failed to decode JSON payload"):
        await simulator.receive_json()


@pytest.mark.asyncio
async def test_push_message_bytes_kind_queues_binary_frame() -> None:
    """push_message with kind='bytes' queues a binary frame."""
    simulator = WebSocketSimulator()

    await simulator.push_message(b"raw", kind="bytes")
    received = await simulator.receive_bytes()

    assert received == b"raw", "the bytes payload must round-trip unchanged"


@pytest.mark.asyncio
async def test_push_message_text_kind_rejects_non_str_payload() -> None:
    """push_message with kind='text' rejects a non-string payload."""
    simulator = WebSocketSimulator()

    with pytest.raises(TypeError, match="Text frames require str payloads"):
        await simulator.push_message(123, kind="text")


@pytest.mark.asyncio
async def test_push_message_bytes_kind_rejects_non_bytes_payload() -> None:
    """push_message with kind='bytes' rejects a payload that is not bytes-like."""
    simulator = WebSocketSimulator()

    with pytest.raises(TypeError, match="Binary frames require bytes payloads"):
        await simulator.push_message("not bytes", kind="bytes")


@pytest.mark.asyncio
async def test_push_bytes_converts_bytearray_to_bytes() -> None:
    """push_bytes converts a bytearray payload to bytes before queueing."""
    simulator = WebSocketSimulator()

    await simulator.push_bytes(bytearray(b"converted"))
    received = await simulator.receive_bytes()

    assert received == b"converted", "the queued frame must be plain bytes"
    assert isinstance(received, bytes), "the queued frame type must be bytes"
