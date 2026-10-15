"""Own accepted WebSocket sessions for the opt-in router receive loop.

``WebSocketRouter(receive_loop=True)`` enters this owner after ``on_connect``
succeeds and before acceptance completes. It dispatches received frames in
order and invokes disconnect hooks and resource cleanup exactly once on exit,
including after failed acceptance or cancellation.
"""

from __future__ import annotations

import contextlib
import logging
import typing as typ

import falcon

from ._observability import _observe_operation
from .diagnostics import _type_name
from .dispatcher import msjson

if typ.TYPE_CHECKING:
    import types

    from .hooks import HookManager
    from .protocols import WebSocketLike
    from .resource import WebSocketResource

logger = logging.getLogger(__name__)


class _ConnectionSession:
    """Own ordered frame dispatch and exactly-once disconnect cleanup."""

    def __init__(
        self,
        resource: WebSocketResource,
        ws: WebSocketLike,
        hook_manager: HookManager,
    ) -> None:
        self.resource = resource
        self.ws = ws
        self.hook_manager = hook_manager
        self.close_code = 1006
        self.has_disconnected = False

    async def __aenter__(self) -> _ConnectionSession:
        """Return the owner before the router completes acceptance."""
        return self

    async def __aexit__(
        self,
        _exc_type: type[BaseException] | None,
        exc: BaseException | None,
        _traceback: types.TracebackType | None,
    ) -> bool:
        """Run disconnect hooks and preserve a failure from the session."""
        try:
            await self._disconnect_once()
        except Exception as cleanup_error:
            if exc is None:
                raise
            exc.add_note(f"Disconnect cleanup failed ({_type_name(cleanup_error)})")
        return False

    async def _disconnect_once(self) -> None:
        """Invoke cleanup at most once, even when a hook or callback fails."""
        if self.has_disconnected:
            return

        self.has_disconnected = True
        with _observe_operation(logger, "disconnect"):
            try:
                await self.hook_manager.notify_before_disconnect(
                    self.resource,
                    ws=self.ws,
                    close_code=self.close_code,
                )
            finally:
                await self.resource.on_disconnect(self.ws, self.close_code)

    async def receive_frames(self) -> None:
        """Dispatch sequential frames until disconnect or an ordinary error."""
        while True:
            try:
                frame = await self.ws.receive_media()
                raw = frame if isinstance(frame, (str, bytes)) else msjson.encode(frame)
                await self.resource.dispatch(self.ws, raw)
            except falcon.WebSocketDisconnected as exc:
                self.close_code = getattr(exc, "code", None) or 1006
                return
            except Exception as exc:  # ruff: ignore[blind-except] - session errors close locally without preventing disconnect cleanup
                logger.warning("WebSocket session failed (%s)", _type_name(exc))
                with contextlib.suppress(Exception):
                    await self.ws.close()
                self.close_code = 1000
                return
