"""Internal harness primitives shared across websocket testing helpers."""

from __future__ import annotations

import collections as cl
import dataclasses as dc
from types import SimpleNamespace

import falcon

from .testing._common import _LifecycleSocket
from .testing.simulator import WebSocketSimulator


class _OriginalWebSocket(_LifecycleSocket):
    """Minimal stub representing the ASGI-provided websocket."""

    def __init__(self, frames: tuple[object, ...] = ()) -> None:
        super().__init__()
        self.sent: list[object] = []
        self._frames: cl.deque[object] = cl.deque(frames)

    async def send_media(  # pylint: disable=trivial-attribute-wrapper  # protocol stub
        self, data: object
    ) -> None:
        self.sent.append(data)  # pragma: no cover - unused

    async def receive_media(self) -> object:
        if self.closed and not self._frames:
            raise falcon.WebSocketDisconnected(self.close_code)
        if self._frames:
            frame = self._frames.popleft()
            if isinstance(frame, BaseException):
                raise frame
            return frame
        raise falcon.WebSocketDisconnected(1000)


class _HarnessSimulator(WebSocketSimulator):
    """Simulator variant that mirrors lifecycle events to the original stub."""

    # pylint: disable-next=trivial-attribute-wrapper  # deliberate seam: testing.harness binds the ASGI stub through this name to keep the intent explicit
    def bind_original(self, original: _OriginalWebSocket) -> None:
        """Associate ``original`` so lifecycle events stay in sync."""
        self.bind_peer(original)


@dc.dataclass(slots=True)
class _TestRequest:
    """Lightweight stand-in for :class:`falcon.Request`."""

    path: str
    path_template: str
    context: SimpleNamespace = dc.field(default_factory=SimpleNamespace)
