"""Internal harness primitives shared across websocket testing helpers."""

from __future__ import annotations

import asyncio
import dataclasses as dc
import typing as typ
from types import SimpleNamespace

from .testing._common import _ORIGINAL_WS_RECEIVE_MSG, _LifecycleSocket
from .testing.simulator import WebSocketSimulator

if typ.TYPE_CHECKING:
    import collections.abc as cabc


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

    async def send_text(self, message: str) -> None:
        """Send a text frame through the original stub."""
        await self.send_media(message)

    async def receive_text(self) -> str:
        """Receive a text frame from the original stub."""
        message = await self.receive_media()
        if not isinstance(message, str):
            msg = "expected a text WebSocket frame"
            raise TypeError(msg)
        return message


class _HarnessSimulator(WebSocketSimulator):
    """Simulator variant that mirrors lifecycle events to the original stub."""

    def __init__(self) -> None:
        super().__init__()
        self.ready_event = asyncio.Event()

    async def _signal_ready_after(self, operation: cabc.Awaitable[None]) -> None:
        """Wake readiness waiters after a mirrored lifecycle operation."""
        try:
            await operation
        finally:
            self.ready_event.set()

    async def accept(
        self, subprotocol: str | None = None, headers: object = None
    ) -> None:
        """Signal readiness after the simulator mirrors handshake acceptance."""
        await self._signal_ready_after(super().accept(subprotocol, headers))

    async def close(self, code: int | None = 1000, reason: str | None = None) -> None:
        """Signal readiness after the simulator mirrors connection closure."""
        await self._signal_ready_after(super().close(code, reason))

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
