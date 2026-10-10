"""Unit tests for the WebSocketSimulator helper."""

from __future__ import annotations

import asyncio

import falcon
import msgspec.json as msjson
import pytest
from hypothesis import example, given
from hypothesis import strategies as st

from falcon_pachinko._disconnect import _PeerDisconnectedError
from falcon_pachinko.testing import WebSocketSimulator

_ASSERTION_FAILURE = "lifecycle expectation failed"


@pytest.mark.asyncio
async def test_accept_and_close_record_state() -> None:
    """Accepting and closing the simulator updates lifecycle flags."""
    simulator = WebSocketSimulator()

    assert simulator.accepted is False, "a new simulator must start unaccepted"
    assert simulator.closed is False, "a new simulator must start open"

    await simulator.accept(subprotocol="chat")
    assert simulator.accepted is True, "accept() must mark the simulator accepted"
    assert simulator.subprotocol == "chat", (
        "the negotiated subprotocol must be recorded"
    )

    await simulator.close(code=1011)
    assert simulator.closed is True, "close() must mark the simulator closed"
    assert simulator.close_code == 1011, "close() must record the given close code"


@pytest.mark.asyncio
async def test_push_disconnect_raises_internal_signal_and_persists_code() -> None:
    """Queued disconnects close the simulator and remain visible to receivers."""
    simulator = WebSocketSimulator()
    await simulator.push_text("before disconnect")
    await simulator.push_disconnect(code=1001)

    assert await simulator.receive_text() == "before disconnect", (
        "queued frames must be received before the disconnect marker"
    )
    with pytest.raises(_PeerDisconnectedError) as first:
        await simulator.receive_media()
    with pytest.raises(_PeerDisconnectedError) as second:
        await simulator.receive_media()

    assert first.value.code == second.value.code == 1001, (
        "later receives must repeat the original disconnect code"
    )
    assert simulator.closed is True, "a peer disconnect must close the simulator"
    assert simulator.close_code == 1001, "the simulator must retain the peer code"


@pytest.mark.asyncio
async def test_send_media_records_outbound_payloads() -> None:
    """Sending payloads stores them for later inspection."""
    simulator = WebSocketSimulator()

    await simulator.send_media({"type": "pong"})
    await simulator.send_json({"type": "json"})

    assert simulator.sent_messages == [
        {"type": "pong"},
        msjson.encode({"type": "json"}),
    ], "both raw and JSON-encoded payloads must be recorded in order"
    assert simulator.pop_sent() == {"type": "pong"}, (
        "popping the first sent message must return the raw payload"
    )
    payload = await simulator.next_sent()
    assert isinstance(payload, bytes), "send_json must enqueue an encoded JSON frame"
    assert msjson.decode(payload) == {"type": "json"}, (
        "the next sent payload must decode to the JSON message"
    )


@pytest.mark.asyncio
async def test_push_and_receive_json_payload() -> None:
    """Queued JSON payloads are decoded when received."""
    simulator = WebSocketSimulator()

    await simulator.push_json({"type": "ping"})
    payload = await simulator.receive_json()

    assert payload == {"type": "ping"}, "the queued JSON payload must be decoded"
    assert simulator.received_messages == [msjson.Encoder().encode({"type": "ping"})], (
        "the raw frame recorded must be exactly the bytes push_json encoded"
    )


@given(messages=st.lists(st.text(), max_size=10))
@example(messages=["hello"])
def test_push_and_receive_text_round_trips_in_order(messages: list[str]) -> None:
    """Queued text payloads round-trip unchanged and in FIFO order."""

    async def _exercise() -> list[str]:
        simulator = WebSocketSimulator()
        for message in messages:
            await simulator.push_text(message)
        return [await simulator.receive_text() for _ in messages]

    received = asyncio.run(_exercise())
    assert received == messages, (
        "queued text payloads must round-trip unchanged, in order"
    )


@pytest.mark.asyncio
async def test_connected_context_manages_lifecycle() -> None:
    """The connected context automatically accepts and closes the simulator."""
    simulator = WebSocketSimulator()

    async with simulator.connected():
        assert simulator.accepted is True, "the context must accept the simulator"
        assert simulator.closed is False, (
            "the simulator must stay open inside the block"
        )

    assert simulator.closed is True, "the context must close the simulator on exit"
    assert simulator.close_code == 1000, "the default close code must be used"


@pytest.mark.asyncio
async def test_close_wakes_pending_receive_and_preserves_first_code() -> None:
    """Closing a simulator releases receive with its first close code."""
    simulator = WebSocketSimulator()
    receive_task = asyncio.create_task(simulator.receive_media())
    await asyncio.sleep(0)

    await simulator.close(4001)
    await simulator.close(4002)

    with pytest.raises(falcon.WebSocketDisconnected) as error:
        await asyncio.wait_for(receive_task, timeout=1)
    assert error.value.code == 4001, _ASSERTION_FAILURE
    assert simulator.close_code == 4001, _ASSERTION_FAILURE


@pytest.mark.asyncio
async def test_receive_after_close_raises_with_recorded_code() -> None:
    """A closed simulator never blocks a later receive call."""
    simulator = WebSocketSimulator()
    await simulator.close(4003)

    with pytest.raises(falcon.WebSocketDisconnected) as error:
        await asyncio.wait_for(simulator.receive_media(), timeout=1)
    assert error.value.code == 4003, _ASSERTION_FAILURE


@pytest.mark.asyncio
async def test_pending_counts_reflect_queue_sizes() -> None:
    """Pending helpers report the number of queued frames."""
    simulator = WebSocketSimulator()

    await simulator.push_text("queued")
    await simulator.send_media("sent")

    assert simulator.pending_inbound() == 1, "one inbound frame must be queued"
    assert simulator.pending_outbound() == 1, "one outbound frame must be queued"


@pytest.mark.asyncio
async def test_close_wakes_a_blocked_receive_with_disconnect_code() -> None:
    """Closing the simulator releases a task waiting for its next frame."""
    simulator = WebSocketSimulator()
    pending_receive = asyncio.create_task(simulator.receive_media())
    await asyncio.sleep(0)

    await simulator.close(code=4001)

    with pytest.raises(_PeerDisconnectedError) as caught:
        await asyncio.wait_for(pending_receive, timeout=1)
    assert caught.value.code == 4001, "the wake-up must preserve the close code"


@pytest.mark.asyncio
async def test_receive_after_close_raises_disconnect() -> None:
    """Later receives report the already-recorded close code."""
    simulator = WebSocketSimulator()
    await simulator.close(code=4002)

    with pytest.raises(_PeerDisconnectedError) as caught:
        await simulator.receive_text()
    assert caught.value.code == 4002, "closed sockets must keep reporting their code"


@pytest.mark.asyncio
async def test_queued_frames_are_delivered_before_disconnect() -> None:
    """Frames queued before close are drained before the disconnect sentinel."""
    simulator = WebSocketSimulator()
    await simulator.push_text("first")
    await simulator.push_bytes(b"second")
    await simulator.close(code=4003)

    assert await simulator.receive_text() == "first", (
        "the first queued frame must be delivered"
    )
    assert await simulator.receive_bytes() == b"second", (
        "the second queued frame must be delivered"
    )
    with pytest.raises(_PeerDisconnectedError) as caught:
        await simulator.receive_media()
    assert caught.value.code == 4003, "disconnect follows the queued frames"
