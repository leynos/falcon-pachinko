"""Unit tests for reference-resource disconnect and envelope behaviour."""

from __future__ import annotations

import pytest

from examples.reference_app.resources import TaskStreamResource
from falcon_pachinko.websocket import WebSocketConnectionManager
from tests._reference_example_support import (
    _PROJECT,
    _WORKSPACE,
    _build_resource,
    _connect_request,
    _dispatch,
)
from tests._stubs import RecordingWebSocket


@pytest.mark.asyncio
async def test_on_disconnect_is_a_noop_when_never_connected() -> None:
    """Disconnecting a resource that never connected does nothing."""
    resource, _repo, audit, _feed = _build_resource()

    await resource.on_disconnect(RecordingWebSocket(), close_code=1000)

    assert not audit.records, "no audit record is written without a connection"


@pytest.mark.asyncio
async def test_on_disconnect_removes_connection_and_audits_closure() -> None:
    """Disconnecting a connected resource removes it and audits the closure."""
    conn_mgr = WebSocketConnectionManager()
    resource, _, audit, _ = _build_resource(conn_mgr)
    ws = RecordingWebSocket()
    await resource.on_connect(
        _connect_request(), ws, workspace_id=_WORKSPACE, project_id=_PROJECT
    )
    conn_id = resource._conn_id
    assert conn_id is not None, "on_connect must assign a connection ID"
    assert conn_mgr.websockets == {conn_id: ws}, (
        "on_connect must register the effective socket"
    )
    assert not ws.unaccepted, "on_connect must accept before readiness"
    assert ws.messages == [
        {
            "type": "session.ready",
            "payload": {"workspace": _WORKSPACE, "project": _PROJECT},
        }
    ], "on_connect must send the session-ready event after acceptance"
    opened = [record for record in audit.records if record["event"] == "session.open"]
    assert len(opened) == 1, "on_connect must record exactly one session.open event"
    opened_metadata = opened[0]["metadata"]
    assert isinstance(opened_metadata, dict), "open metadata must be a mapping"
    assert opened_metadata["connection"] == conn_id, "open must carry this connection"

    await resource.on_disconnect(ws, close_code=1001)
    await resource.on_disconnect(ws, close_code=1001)

    assert conn_mgr.websockets == {}, "the connection must be removed on disconnect"
    assert resource._conn_id is None, "disconnect must clear the resource identity"
    closed = [r for r in audit.records if r["event"] == "session.closed"]
    assert len(closed) == 1, f"exactly one session.closed record is expected: {closed}"
    closed_metadata = closed[0]["metadata"]
    assert isinstance(closed_metadata, dict), "audit metadata must be a mapping"
    assert closed_metadata["connection"] == conn_id, (
        "close must carry the same connection"
    )
    assert closed_metadata["close_code"] == 1001, "close_code must match"
    assert closed_metadata["workspace"] == _WORKSPACE, "workspace must match"
    assert closed_metadata["project"] == _PROJECT, "project must match"


class EnvelopeTaskStreamResource(TaskStreamResource):
    """``TaskStreamResource`` without a schema, so envelopes are decoded.

    The reference deployment compiles its inbound payloads before they reach
    the WebSocket loop. A deployment that skips that compilation still hands
    the same class to ``dispatch()``, and the envelope path is then peer
    steerable. This subclass keeps the real lifecycle and collaborators while
    forcing that path.
    """

    schema = None


def _envelope_resource(
    conn_mgr: WebSocketConnectionManager,
) -> EnvelopeTaskStreamResource:
    """Build an envelope-dispatching task stream sharing real collaborators."""
    resource, _repo, _audit, _feed = _build_resource(conn_mgr)
    return EnvelopeTaskStreamResource(
        workspace_repo=resource._repo,
        audit_trail=resource._audit,
        announcement_feed=resource._feed,
        conn_mgr=conn_mgr,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("tag", ["disconnect", "Disconnect", "DISCONNECT"])
async def test_reserved_envelope_tag_leaves_live_membership_intact(tag: str) -> None:
    """A ``disconnect`` envelope cannot evict a live connection.

    The payload is a valid close code, so nothing but the reserved-name
    refusal stops ``on_disconnect`` from tearing down a connected session.
    """
    conn_mgr = WebSocketConnectionManager()
    resource = _envelope_resource(conn_mgr)
    ws = RecordingWebSocket()
    await resource.on_connect(
        _connect_request(), ws, workspace_id=_WORKSPACE, project_id=_PROJECT
    )
    await resource.on_accepted(ws)

    await _dispatch(resource, ws, {"type": tag, "payload": 1000})

    assert dict(conn_mgr.websockets) == {resource._conn_id: ws}, (
        f"the live connection must survive a reserved envelope: {conn_mgr.websockets}"
    )
    assert dict(conn_mgr.rooms) == {f"workspace:{_WORKSPACE}": {resource._conn_id}}, (
        f"workspace membership must be unchanged: {conn_mgr.rooms}"
    )
    assert not [r for r in resource._audit.records if r["event"] == "session.closed"], (
        "a reserved envelope must not audit a closure"
    )
    assert ws.messages[-1] == {
        "type": "error",
        "payload": "unsupported message",
    }, f"the application fallback must answer the reserved envelope: {ws.messages}"


@pytest.mark.asyncio
async def test_reserved_envelope_cannot_evict_through_real_lifecycle() -> None:
    """A ``disconnect`` envelope cannot reach the real cleanup callback.

    The close code is type-correct for ``on_disconnect``, so only the
    reserved-name refusal stands between the frame and a live-connection
    teardown.
    """
    conn_mgr = WebSocketConnectionManager()
    resource = _envelope_resource(conn_mgr)
    ws = RecordingWebSocket()
    await resource.on_connect(
        _connect_request(), ws, workspace_id=_WORKSPACE, project_id=_PROJECT
    )
    await resource.on_accepted(ws)
    conn_id = resource._conn_id

    await _dispatch(resource, ws, {"type": "disconnect", "payload": 1000})

    assert dict(conn_mgr.websockets) == {conn_id: ws}, (
        f"the live connection must survive: {conn_mgr.websockets}"
    )
    assert dict(conn_mgr.rooms) == {f"workspace:{_WORKSPACE}": {conn_id}}, (
        f"workspace membership must be unchanged: {conn_mgr.rooms}"
    )
    assert not [r for r in resource._audit.records if r["event"] == "session.closed"], (
        "the reserved envelope must not audit a closure"
    )
    assert ws.messages[-1] == {
        "type": "error",
        "payload": "unsupported message",
    }, f"the application fallback must answer the frame: {ws.messages}"

    # The session must still be usable afterwards, not merely reported live.
    await _dispatch(resource, ws, {"type": "task.list", "payload": {}})
    last = ws.messages[-1]
    assert isinstance(last, dict), f"the reply must be a mapping: {ws.messages}"
    assert last["type"] == "task.list", (
        f"a genuine message must still be served: {ws.messages}"
    )


@pytest.mark.asyncio
async def test_connect_envelope_tag_does_not_reconnect() -> None:
    """A ``connect`` envelope cannot re-run the connect lifecycle."""
    conn_mgr = WebSocketConnectionManager()
    resource = _envelope_resource(conn_mgr)
    ws = RecordingWebSocket()
    await resource.on_connect(
        _connect_request(), ws, workspace_id=_WORKSPACE, project_id=_PROJECT
    )
    await resource.on_accepted(ws)
    ready_count = len(ws.messages)

    await _dispatch(resource, ws, {"type": "connect", "payload": True})

    assert len(conn_mgr.websockets) == 1, (
        "connect must not register a second connection"
    )
    assert len(ws.messages) == ready_count + 1, (
        f"only the fallback reply is expected: {ws.messages}"
    )
    assert ws.messages[-1] == {
        "type": "error",
        "payload": "unsupported message",
    }, f"the connect tag must fall back, not send a second session.ready: {ws.messages}"
