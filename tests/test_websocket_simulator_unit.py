"""Unit tests for the WebSocketSimulator helper."""

from __future__ import annotations

import msgspec.json as msjson
import pytest

from falcon_pachinko.testing import WebSocketSimulator


@pytest.mark.asyncio
async def test_accept_and_close_record_state() -> None:
    """Accepting and closing the simulator updates lifecycle flags."""
    simulator = WebSocketSimulator()

    assert simulator.accepted is False
    assert simulator.closed is False

    await simulator.accept(subprotocol="chat")
    assert simulator.accepted is True
    assert simulator.subprotocol == "chat"

    await simulator.close(code=1011)
    assert simulator.closed is True
    assert simulator.close_code == 1011


@pytest.mark.asyncio
async def test_send_media_records_outbound_payloads() -> None:
    """Sending payloads stores them for later inspection."""
    simulator = WebSocketSimulator()

    await simulator.send_media({"type": "pong"})
    await simulator.send_json({"type": "json"})

    assert simulator.sent_messages == [
        {"type": "pong"},
        msjson.encode({"type": "json"}),
    ]
    assert simulator.pop_sent() == {"type": "pong"}
    payload = await simulator.next_sent()
    if isinstance(payload, bytes | bytearray | memoryview):
        buffer = bytes(payload)
    else:
        buffer = msjson.encode(payload)
    assert msjson.decode(buffer) == {"type": "json"}


@pytest.mark.asyncio
async def test_push_and_receive_json_payload() -> None:
    """Queued JSON payloads are decoded when received."""
    simulator = WebSocketSimulator()

    await simulator.push_json({"type": "ping"})
    payload = await simulator.receive_json()

    assert payload == {"type": "ping"}
    assert simulator.received_messages  # raw bytes recorded


@pytest.mark.asyncio
async def test_push_and_receive_text_payload() -> None:
    """Text payloads round-trip without conversion."""
    simulator = WebSocketSimulator()

    await simulator.push_text("hello")
    assert await simulator.receive_text() == "hello"


@pytest.mark.asyncio
async def test_connected_context_manages_lifecycle() -> None:
    """The connected context automatically accepts and closes the simulator."""
    simulator = WebSocketSimulator()

    async with simulator.connected():
        assert simulator.accepted is True
        assert simulator.closed is False

    assert simulator.closed is True
    assert simulator.close_code == 1000


@pytest.mark.asyncio
async def test_pending_counts_reflect_queue_sizes() -> None:
    """Pending helpers report the number of queued frames."""
    simulator = WebSocketSimulator()

    await simulator.push_text("queued")
    await simulator.send_media("sent")

    assert simulator.pending_inbound() == 1
    assert simulator.pending_outbound() == 1
