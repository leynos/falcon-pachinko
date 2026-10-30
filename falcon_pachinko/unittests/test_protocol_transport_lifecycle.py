"""Acceptance and cleanup for transports exposing only WebSocketLike methods."""

from __future__ import annotations

import collections as cl
import typing as typ

import falcon
import pytest

from falcon_pachinko.resource import WebSocketResource
from falcon_pachinko.router import WebSocketRouter
from falcon_pachinko.unittests.helpers import make_req

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from falcon_pachinko.protocols import WebSocketLike


class ProtocolSocket:
    """Record protocol calls without exposing Falcon's acceptance probe."""

    def __init__(self, frame: bytes) -> None:
        self.frames: cl.deque[bytes] = cl.deque((frame,))
        self.closed = False
        self.close_code = 1000
        self.accept_calls = 0
        self.sent_media: list[object] = []

    async def accept(
        self,
        subprotocol: str | None = None,
        headers: cabc.Mapping[str, str] | cabc.Iterable[tuple[str, str]] | None = None,
    ) -> None:
        """Record a successful handshake without a public acceptance flag."""
        del subprotocol, headers
        self.accept_calls += 1

    async def close(self, code: int | None = 1000, reason: str | None = None) -> None:
        """Preserve the first transport close code."""
        del reason
        if not self.closed:
            self.closed = True
            self.close_code = 1000 if code is None else code

    # pylint: disable-next=trivial-attribute-wrapper  # the public protocol method must record sends without depending on Falcon state
    async def send_media(self, media: object) -> None:
        """Record replies sent through the protocol facade."""
        self.sent_media.append(media)

    async def receive_media(self) -> object:
        """Yield seeded media or the effective disconnect signal."""
        if not self.closed and self.frames:
            return self.frames.popleft()
        raise falcon.WebSocketDisconnected(self.close_code)

    # pylint: disable-next=trivial-attribute-wrapper  # protocol stub: this method records text frames for WebSocketLike callers
    async def send_text(self, message: str) -> None:
        """Record text frames using the media observation list."""
        self.sent_media.append(message)

    async def receive_text(self) -> str:
        """Receive and validate a text frame."""
        message = await self.receive_media()
        if not isinstance(message, str):
            msg = "expected a text WebSocket frame"
            raise TypeError(msg)
        return message


class ProtocolResource(WebSocketResource):
    """Track membership across approval, resource acceptance, and setup failure."""

    def __init__(self, mode: str, failure: RuntimeError) -> None:
        self.mode = mode
        self.failure = failure
        self.members: set[int] = set()
        self.disconnect_codes: list[int] = []
        self.handled_frames: list[str | bytes] = []

    async def on_connect(
        self, req: falcon.Request, ws: WebSocketLike, **params: object
    ) -> bool:
        """Exercise approval, acceptance, rejection, and setup failure."""
        del req, params
        if self.mode == "reject":
            return False
        self.members.add(id(ws))
        if self.mode == "router":
            return True
        await ws.accept()
        if self.mode == "resource_failure":
            raise self.failure
        return False

    async def on_disconnect(self, ws: WebSocketLike, close_code: int) -> None:
        """Release membership through the router's cleanup promise."""
        self.members.remove(id(ws))
        self.disconnect_codes.append(close_code)

    async def on_unhandled(self, ws: WebSocketLike, message: str | bytes) -> None:
        """Exercise receive and send calls on the supplied socket."""
        self.handled_frames.append(message)
        await ws.send_media({"type": "reply"})


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "expected_codes", "expected_accepts"),
    [
        pytest.param("router", [1000], 1, id="router"),
        pytest.param("resource_reject", [1000], 1, id="resource_reject"),
        pytest.param("resource_failure", [1011], 1, id="resource_failure"),
        pytest.param("reject", [], 0, id="reject"),
    ],
)
async def test_transport_without_acceptance_state(
    mode: str, expected_codes: list[int], expected_accepts: int
) -> None:
    """Keep protocol-only transports usable and clean up observed acceptance."""
    failure = RuntimeError("accepted protocol transport setup failed")
    resource = ProtocolResource(mode, failure)
    router = WebSocketRouter(resource_factory=lambda _factory: resource)
    router.add_route("/session", ProtocolResource)
    router.mount("/")
    frame = b'{"type":"protocol-only"}'
    socket = ProtocolSocket(frame)
    if mode == "resource_failure":
        with pytest.raises(RuntimeError, match="protocol transport") as raised:
            await router.on_websocket(make_req("/session", "/"), socket)
        assert raised.value is failure, "accepted setup must preserve its failure"
    else:
        await router.on_websocket(make_req("/session", "/"), socket)
    assert resource.disconnect_codes == expected_codes, "cleanup must follow acceptance"
    assert not resource.members, "observed acceptance must not leave membership behind"
    assert socket.accept_calls == expected_accepts, "acceptance must not be duplicated"
    expected_frames = [frame] if mode == "router" else []
    assert resource.handled_frames == expected_frames, "dispatch must follow acceptance"
    expected_replies = [{"type": "reply"}] if mode == "router" else []
    assert socket.sent_media == expected_replies, "media sending must remain usable"
