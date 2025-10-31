"""Internal harness primitives shared across websocket testing helpers."""

from __future__ import annotations

import dataclasses as dc
import typing as typ
from types import SimpleNamespace

from .testing._common import _ORIGINAL_WS_RECEIVE_MSG, _LifecycleSocket
from .testing.simulator import WebSocketSimulator


class _OriginalWebSocket(_LifecycleSocket):
    """Minimal stub representing the ASGI-provided websocket."""

    def __init__(self) -> None:
        super().__init__()
        self.sent: list[object] = []

    async def accept(self, subprotocol: str | None = None) -> None:
        await super().accept(subprotocol=subprotocol)

    async def close(self, code: int = 1000) -> None:
        await super().close(code)

    async def send_media(self, data: object) -> None:  # pragma: no cover - unused
        self.sent.append(data)

    async def receive_media(self) -> object:  # pragma: no cover - unused
        raise RuntimeError(_ORIGINAL_WS_RECEIVE_MSG)


class _HarnessSimulator(WebSocketSimulator):
    """Simulator variant that mirrors lifecycle events to the original stub."""

    def __init__(self) -> None:
        super().__init__()
        self._original: _OriginalWebSocket | None = None

    def bind_original(self, original: _OriginalWebSocket) -> None:
        """Associate ``original`` so lifecycle events stay in sync."""
        self._original = original
        self.bind_peer(original)


@dc.dataclass(slots=True)
class _TestRequest:
    """Lightweight stand-in for :class:`falcon.Request`."""

    path: str
    path_template: str
    context: SimpleNamespace = dc.field(default_factory=SimpleNamespace)


if typ.TYPE_CHECKING:
    from .testing import SimulatorRouterHarness

try:  # pragma: no cover - optional dependency for fixture registration
    import pytest
except ImportError:  # pragma: no cover - fixture only available under pytest
    pytest = None  # type: ignore[assignment]
else:

    @pytest.fixture
    def websocket_simulator() -> typ.Iterator[SimulatorRouterHarness]:
        """Provide a router harness pre-wired with a simulator factory."""
        from .testing import SimulatorRouterHarness

        harness = SimulatorRouterHarness()
        try:
            yield harness
        finally:
            harness._pending_simulator = None
