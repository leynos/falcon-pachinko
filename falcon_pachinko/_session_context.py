"""Focused inputs for session execution and disconnect cleanup."""

from __future__ import annotations

import dataclasses as dc
import typing as typ

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    import falcon

    from ._session import SessionState
    from ._session_instrumentation import _SessionInstrumentationRun
    from .hooks import HookManager
    from .lifecycle import LifecycleGuard
    from .observability import LifecycleObserver
    from .protocols import DisconnectClassifier, FrameReceiver
    from .resource import WebSocketResource


@dc.dataclass(frozen=True, slots=True)
class SessionObservationAdapter:
    """Bundle optional telemetry dependencies outside lifecycle domain state."""

    observer: LifecycleObserver | None = None
    monotonic_clock: cabc.Callable[[], float] | None = None
    instrumentation_run: _SessionInstrumentationRun | None = None


@dc.dataclass(slots=True)
class SessionDriverContext:
    """Supply only the resource and receive dependencies used by session work."""

    resource: WebSocketResource
    websocket: LifecycleGuard
    session_state: SessionState
    hook_manager: HookManager
    params: cabc.Mapping[str, object]
    disconnect_classifier: DisconnectClassifier
    frame_receiver: FrameReceiver
    observation: SessionObservationAdapter


@dc.dataclass(slots=True)
class SessionCleanupContext:
    """Supply the request, guarded socket, and policy needed for cleanup only."""

    resource: WebSocketResource
    req: falcon.Request
    websocket: LifecycleGuard
    session_state: SessionState
    hook_manager: HookManager
    params: cabc.Mapping[str, object]
    observation: SessionObservationAdapter
    primary_error: BaseException | None = None
