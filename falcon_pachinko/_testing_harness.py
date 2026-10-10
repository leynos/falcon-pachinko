"""Internal harness primitives shared across websocket testing helpers."""

from __future__ import annotations

import asyncio
import dataclasses as dc
from types import SimpleNamespace

from .testing._common import _ORIGINAL_WS_RECEIVE_MSG, _LifecycleSocket
from .testing.simulator import WebSocketSimulator


class _OriginalWebSocket(_LifecycleSocket):
    """Minimal stub representing the ASGI-provided websocket."""

    def __init__(self) -> None:
        super().__init__()
        self.sent: list[object] = []

    async def send_media(  # pylint: disable=trivial-attribute-wrapper  # protocol stub
        self, data: object
    ) -> None:
        self.sent.append(data)  # pragma: no cover - unused

    async def receive_media(  # ruff: ignore[no-self-use] - protocol needs an instance method
        self,
    ) -> object:
        raise RuntimeError(_ORIGINAL_WS_RECEIVE_MSG)  # pragma: no cover - unused


class _HarnessSimulator(WebSocketSimulator):
    """Simulator variant that mirrors lifecycle events to the original stub."""

    def __init__(self) -> None:
        super().__init__()
        self.ready_event = asyncio.Event()

    async def accept(self, subprotocol: str | None = None) -> None:
        """Signal readiness after the simulator mirrors handshake acceptance."""
        try:
            await super().accept(subprotocol)
        finally:
            self.ready_event.set()

    async def close(self, code: int = 1000) -> None:
        """Signal readiness after the simulator mirrors connection closure."""
        try:
            await super().close(code)
        finally:
            self.ready_event.set()

    # pylint: disable-next=trivial-attribute-wrapper  # deliberate seam: testing.harness binds the ASGI stub through this name to keep the intent explicit
    def bind_original(self, original: _OriginalWebSocket) -> None:
        """Associate ``original`` so lifecycle events stay in sync."""
        self.bind_peer(original)


@dc.dataclass(slots=True)
class _TestRequest:
    """Lightweight stand-in for :class:`falcon.Request`."""

    path: str
    path_template: str
    headers: dict[str, str] = dc.field(default_factory=dict)
    context: SimpleNamespace = dc.field(default_factory=SimpleNamespace)

    def get_header(self, name: str, default: str | None = None) -> str | None:
        """Return a case-insensitive header value for reference hooks."""
        return self.headers.get(name.lower(), default)
