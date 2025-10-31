"""pytest harness for simulator-backed WebSocket routing."""

from __future__ import annotations

import dataclasses as dc
import typing as typ
from contextlib import asynccontextmanager

import falcon.asgi
import msgspec.json as msjson

from falcon_pachinko._testing_harness import (
    _HarnessSimulator,
    _OriginalWebSocket,
    _TestRequest,
)
from falcon_pachinko.router import WebSocketRouter

from ._common import _JSON_FRAME_REQUIRED_MSG, FrameKind

if typ.TYPE_CHECKING:
    from falcon_pachinko.testing.simulator import WebSocketSimulator
else:  # pragma: no cover - optional types under runtime-only execution
    WebSocketSimulator = typ.Any


@dc.dataclass(slots=True)
class SimulatorConnection:
    """Describe a simulated connection managed by the pytest harness."""

    path: str
    router: WebSocketRouter
    simulator: WebSocketSimulator
    request: object
    websocket: _OriginalWebSocket
    _json_decoder: msjson.Decoder = dc.field(init=False, repr=False)

    def __post_init__(self) -> None:
        """Initialize a decoder used to inspect outbound JSON frames."""
        self._json_decoder = msjson.Decoder()

    @property
    def accepted(self) -> bool:
        """Return ``True`` when the simulator accepted the handshake."""
        return self.simulator.accepted

    @property
    def closed(self) -> bool:
        """Return ``True`` once the simulator recorded connection closure."""
        return self.simulator.closed

    @property
    def close_code(self) -> int | None:
        """Expose the close code recorded by the simulator."""
        return self.simulator.close_code

    @property
    def sent_messages(self) -> list[object]:
        """Return a snapshot of frames emitted by the resource."""
        return list(self.simulator.sent_messages)

    @property
    def subprotocol(self) -> str | None:
        """Expose the negotiated subprotocol from the simulator."""
        return self.simulator.subprotocol

    def pop_sent(self) -> object:
        """Pop the next outbound frame without decoding."""
        return self.simulator.pop_sent()

    def pop_sent_json(self, payload_type: type[object] | None = None) -> object:
        """Pop the next outbound frame and decode it as JSON."""
        raw = self.pop_sent()
        if isinstance(raw, str):
            data = raw.encode("utf-8")
        elif isinstance(raw, bytes | bytearray | memoryview):
            data = bytes(raw)
        else:  # pragma: no cover - safeguarded by simulator helpers
            raise TypeError(_JSON_FRAME_REQUIRED_MSG)
        decoder = (
            self._json_decoder if payload_type is None else msjson.Decoder(payload_type)
        )
        return decoder.decode(data)

    async def push_json(self, payload: object) -> None:
        """Queue a JSON payload for the resource to consume."""
        await self.simulator.push_json(payload)

    async def push_text(self, message: str) -> None:
        """Queue a UTF-8 text frame for the resource."""
        await self.simulator.push_text(message)

    async def push_bytes(self, payload: bytes | bytearray | memoryview) -> None:
        """Queue a binary frame for the resource."""
        await self.simulator.push_bytes(payload)


class SimulatorRouterHarness:
    """Manage a simulator-backed router mounted on a Falcon ASGI app."""

    def __init__(self, *, mount: str = "/") -> None:
        self.app = falcon.asgi.App()
        self._mount_prefix = self._normalize_mount(mount)
        self._pending_simulator: WebSocketSimulator | None = None
        self.router = WebSocketRouter(simulator_factory=self._provide_simulator)
        self._mounted = False
        self.mount(self._mount_prefix)

    def mount(self, prefix: str) -> None:
        """Mount the router at ``prefix`` and register it with the app."""
        normalized = self._normalize_mount(prefix)
        if self._mounted:
            if normalized != self._mount_prefix:
                msg = f"router already mounted at {self._mount_prefix!r}"
                raise RuntimeError(msg)
            return
        self.router.mount(normalized)
        self.app.add_route(normalized, self.router)
        self._mount_prefix = normalized
        self._mounted = True

    def _normalize_mount(self, prefix: str) -> str:
        if not prefix:
            return "/"
        if not prefix.startswith("/"):
            prefix = f"/{prefix}"
        return prefix.rstrip("/") or "/"

    def _provide_simulator(self, req: object, ws: object) -> WebSocketSimulator:
        """Return the simulator associated with the next connection."""
        simulator = self._pending_simulator or _HarnessSimulator()
        self._pending_simulator = None
        if isinstance(simulator, _HarnessSimulator) and isinstance(
            ws, _OriginalWebSocket
        ):
            simulator.bind_original(ws)
        return simulator

    def _compose_path(self, path: str) -> str:
        if not path:
            return self._mount_prefix
        if not path.startswith("/"):
            path = f"/{path}"
        if path.startswith(self._mount_prefix):
            return path
        return path if self._mount_prefix == "/" else f"{self._mount_prefix}{path}"

    @asynccontextmanager
    async def connect(
        self,
        path: str,
        *,
        initial_inbound: typ.Iterable[tuple[object, FrameKind]] | None = None,
    ) -> typ.AsyncIterator[SimulatorConnection]:
        """Dispatch ``path`` through the router yielding a connection helper."""
        if not self._mounted:
            msg = "router must be mounted before establishing connections"
            raise RuntimeError(msg)

        simulator = _HarnessSimulator()
        self._pending_simulator = simulator
        if initial_inbound is not None:
            for payload, kind in initial_inbound:
                await simulator.push_message(payload, kind=kind)
        request_path = self._compose_path(path)
        request = _TestRequest(path=request_path, path_template=self._mount_prefix)
        original = _OriginalWebSocket()
        try:
            await self.router.on_websocket(
                typ.cast("falcon.asgi.Request", request),
                original,
            )
            yield SimulatorConnection(
                path=request_path,
                router=self.router,
                simulator=simulator,
                request=request,
                websocket=original,
            )
        finally:
            self._pending_simulator = None
            if not simulator.closed:
                await simulator.close()
            if not original.closed:
                await original.close()
