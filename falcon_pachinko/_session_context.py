"""Shared types for session execution, negotiation, and cleanup."""

from __future__ import annotations

import abc
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
    from .protocols import DisconnectClassifier, FrameReceiver, WebSocketLike
    from .resource import WebSocketResource


class SessionRouteMatch(typ.Protocol):
    """Route captures read by session setup without router dependencies."""

    @property
    @abc.abstractmethod
    def params(self) -> cabc.Mapping[str, str]:
        """Captured URI parameters for the selected resource."""


class SessionConnection(typ.Protocol):
    """Request and socket fields shared by session execution and cleanup."""

    req: falcon.Request
    ws: WebSocketLike


class SessionDispatch(SessionConnection, typ.Protocol):
    """Mutable socket and lifecycle fields supplied by the routing pipeline."""

    session: SessionState | None

    @property
    @abc.abstractmethod
    def match(self) -> SessionRouteMatch:
        """Selected route parameters consumed by the session driver."""


@dc.dataclass(slots=True)
class SessionDriverContext:
    """Retain values shared by session execution and finalization."""

    resource: WebSocketResource
    resources: tuple[WebSocketResource, ...]
    websocket: LifecycleGuard
    dispatch: SessionDispatch
    session_state: SessionState
    hook_manager: HookManager
    params: cabc.Mapping[str, object]
    disconnect_classifier: DisconnectClassifier
    frame_receiver: FrameReceiver
    primary_error: BaseException | None = None
    finalization_error: BaseException | None = None
    observer: LifecycleObserver | None = None
    monotonic_clock: cabc.Callable[[], float] | None = None
    instrumentation_run: _SessionInstrumentationRun | None = None
