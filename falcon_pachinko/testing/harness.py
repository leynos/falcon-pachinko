"""pytest harness for simulator-backed WebSocket routing."""

from __future__ import annotations

import asyncio
import dataclasses as dc
import typing as typ
from contextlib import asynccontextmanager, suppress

import falcon.asgi
import msgspec.json as msjson

from falcon_pachinko._testing_harness import (
    _HarnessSimulator,
    _OriginalWebSocket,
    _TestRequest,
)
from falcon_pachinko.router import WebSocketRouter

from ._common import FrameKind, _decode_json

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from falcon_pachinko.lifecycle import WebSocketSession
    from falcon_pachinko.testing.simulator import WebSocketSimulator


_SESSION_EXIT_TIMEOUT = 1.0


@dc.dataclass(slots=True)
class SimulatorConnection:
    """Describe a simulated connection managed by the pytest harness."""

    path: str
    router: WebSocketRouter
    simulator: WebSocketSimulator
    request: object
    websocket: _OriginalWebSocket
    session: WebSocketSession | None = None
    _json_decoder: msjson.Decoder[object] = dc.field(init=False, repr=False)
    _decoders: dict[type[object], msjson.Decoder[object]] = dc.field(
        init=False, repr=False, default_factory=dict
    )

    def __post_init__(self) -> None:
        """Initialize a decoder used to inspect outbound JSON frames."""
        self._json_decoder = msjson.Decoder()

    @property
    def accepted(self) -> bool:
        """``True`` when the simulator accepted the handshake."""
        return self.simulator.accepted

    @property
    def closed(self) -> bool:
        """``True`` once the simulator recorded connection closure."""
        return self.simulator.closed

    @property
    def close_code(self) -> int | None:
        """Expose the close code recorded by the simulator."""
        return self.simulator.close_code

    @property
    def sent_messages(self) -> list[object]:
        """Snapshot of the frames emitted by the resource."""
        return list(self.simulator.sent_messages)

    @property
    def subprotocol(self) -> str | None:
        """Expose the negotiated subprotocol from the simulator."""
        return self.simulator.subprotocol

    # pylint: disable-next=trivial-attribute-wrapper  # deliberate public facade API: SimulatorConnection hides the simulator's private queues from test authors
    def pop_sent(self) -> object:
        """Pop the next outbound frame without decoding."""
        return self.simulator.pop_sent()

    def pop_sent_json(self, payload_type: type[object] | None = None) -> object:
        """Pop the next outbound frame and decode it as JSON.

        Returns
        -------
        object
            The decoded JSON payload, using ``payload_type`` when supplied.

        Unsupported frames raise ``TypeError`` through the shared decoder.
        """
        raw = self.pop_sent()
        return _decode_json(self._decoder_for(payload_type), raw, payload_type)

    def _decoder_for(self, payload_type: type[object] | None) -> msjson.Decoder[object]:
        """Return a cached decoder for ``payload_type``."""
        if payload_type is None:
            return self._json_decoder
        if (decoder := self._decoders.get(payload_type)) is None:
            decoder = self._decoders[payload_type] = msjson.Decoder(payload_type)
        return decoder

    async def push_json(self, payload: object) -> None:
        """Queue a JSON payload for the resource to consume."""
        await self.simulator.push_json(payload)

    async def push_text(self, message: str) -> None:
        """Queue a UTF-8 text frame for the resource."""
        await self.simulator.push_text(message)

    async def push_bytes(self, payload: bytes | bytearray | memoryview) -> None:
        """Queue a binary frame for the resource."""
        await self.simulator.push_bytes(payload)

    async def next_sent(self) -> object:
        """Wait for a response after the router processes inbound frames."""
        return await self.simulator.next_sent()

    async def wait_for_sent_count(self, count: int) -> None:
        """Wait for a response count without consuming queued frames."""
        await self.simulator.wait_for_sent_count(count)

    async def next_sent_json(self, payload_type: type[object] | None = None) -> object:
        """Wait for and decode the next JSON response."""
        raw = await self.next_sent()
        return _decode_json(self._decoder_for(payload_type), raw, payload_type)


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

    def discard_pending_simulator(self) -> None:
        """Drop any simulator staged for the next connection."""
        self._pending_simulator = None

    @staticmethod
    def _normalize_mount(prefix: str) -> str:
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
        initial_inbound: cabc.Iterable[tuple[object, FrameKind]] | None = None,
    ) -> cabc.AsyncIterator[SimulatorConnection]:
        """Dispatch ``path`` through the router yielding a connection helper.

        Yields
        ------
        SimulatorConnection
            A helper bound to the simulator serving ``path``. An accepted
            session is closed through its lifecycle handle on exit.

        Raises
        ------
        RuntimeError
            If the router has not been mounted yet.
        """
        if not self._mounted:
            msg = "router must be mounted before establishing connections"
            raise RuntimeError(msg)

        simulator = _HarnessSimulator()
        self._pending_simulator = simulator
        await self._seed_inbound(simulator, initial_inbound)
        request_path = self._compose_path(path)
        request = _TestRequest(path=request_path, path_template=self._mount_prefix)
        original = _OriginalWebSocket()
        session: WebSocketSession | None = None
        session_task: asyncio.Task[None] | None = None
        try:
            session = await self.router.negotiate(request, original)
            session_task = await self._start_session(session)
            yield SimulatorConnection(
                path=request_path,
                router=self.router,
                simulator=simulator,
                request=request,
                websocket=original,
                session=session,
            )
        finally:
            self._pending_simulator = None
            await self._close_connection(session, simulator, original, session_task)

    @staticmethod
    async def _seed_inbound(
        simulator: WebSocketSimulator,
        frames: cabc.Iterable[tuple[object, FrameKind]] | None,
    ) -> None:
        """Queue the frames that the simulated client sends after acceptance."""
        if frames is None:
            return
        for payload, kind in frames:
            await simulator.push_message(payload, kind=kind)

    @staticmethod
    async def _start_session(
        session: WebSocketSession | None,
    ) -> asyncio.Task[None] | None:
        """Start post-accept execution without delaying the fixture's yield."""
        if session is None:
            return None
        task = asyncio.create_task(session.run())
        # Let post-accept setup start before exposing the connection; setup may
        # then wait for client frames without blocking the fixture's yield.
        await asyncio.sleep(0)
        return task

    @staticmethod
    async def _close_connection(
        session: WebSocketSession | None,
        simulator: WebSocketSimulator,
        original: _OriginalWebSocket,
        session_task: asyncio.Task[None] | None,
    ) -> None:
        """Finalize the session and its paired test transports on exit."""
        try:
            if session is not None:
                await session.ws.close()
                cancelled_for_exit = False
                if session_task is not None and not session_task.done():
                    await asyncio.wait({session_task}, timeout=_SESSION_EXIT_TIMEOUT)
                    if not session_task.done():
                        session_task.cancel()
                        cancelled_for_exit = True
                        with suppress(asyncio.CancelledError):
                            await session_task
                SimulatorRouterHarness._retrieve_task_exception(session_task)
                try:
                    await session.aclose()
                except asyncio.CancelledError:
                    current_task = asyncio.current_task()
                    cancellation_is_requested = (
                        current_task is not None and current_task.cancelling() > 0
                    )
                    should_propagate_cancellation = (
                        not cancelled_for_exit or cancellation_is_requested
                    )
                    if should_propagate_cancellation:
                        raise
            elif not simulator.closed:
                await simulator.close(1000)
        finally:
            if not original.closed:
                await original.close()

    @staticmethod
    def _retrieve_task_exception(session_task: asyncio.Task[None] | None) -> None:
        """Retrieve a completed task error not consumed by session.aclose()."""
        if session_task is None:
            return
        if not session_task.done() or session_task.cancelled():
            return
        # aclose() waits on session completion, not on this task.
        session_task.exception()
