"""Own the opt-in WebSocket acceptance phase and transfer accepted sessions.

``WebSocketRouter(receive_loop=True)`` enters this owner after ``on_connect``
allows the connection and before acceptance completes. If acceptance setup
fails, it runs disconnect hooks and attempts resource cleanup with close code
1006. A peer disconnect during post-accept setup uses the peer's close code;
unexpected post-accept failures close with 1011. Once acceptance succeeds, it
transfers frame reception and disconnect cleanup to the shared
``session.drive_session`` loop when enabled. During cleanup, hooks precede the
resource callback, which is attempted once even if cancellation interrupts it.
The shared driver may retry a hook interrupted by cancellation before the
callback, while preventing a second callback attempt.
"""

from __future__ import annotations

import asyncio
import logging
import typing as typ

import falcon

from ._observability import _observe_operation
from .diagnostics import _type_name
from .session import _close_after_failure, drive_session

if typ.TYPE_CHECKING:
    import collections.abc as cabc
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
        """Own cleanup until acceptance transfers the session to its driver."""
        self.resource = resource
        self.ws = ws
        self.hook_manager = hook_manager
        self.close_code = 1006
        self.has_disconnected = False
        self.has_accepted = False

    def mark_accepted(self) -> None:
        """Record successful acceptance before post-accept setup begins."""
        self.has_accepted = True

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

    async def run(
        self,
        acceptance: cabc.Awaitable[bool],
        *,
        on_policy_close: cabc.Callable[[], None],
        receive_loop: bool = True,
    ) -> None:
        """Protect acceptance, then transfer to the configured session owner."""
        async with self:
            try:
                await acceptance
            except falcon.WebSocketDisconnected as disconnected:
                self.close_code = (
                    disconnected.code if disconnected.code is not None else 1006
                )
                return
            except asyncio.CancelledError:
                if self.has_accepted:
                    self.close_code = 1006
                raise
            except Exception:
                if self.has_accepted:
                    self.close_code = 1011
                    on_policy_close()
                    await _close_after_failure(self.ws, self.close_code)
                raise

            if getattr(self.ws, "closed", False):
                self.close_code = 1006
                return
            if not receive_loop:
                # Explicit opt-out preserves the application's receive and
                # disconnect ownership after setup has succeeded.
                self.has_disconnected = True
                return

            resource, ws, hook_manager = self.resource, self.ws, self.hook_manager
            # There is no suspension between transfer and entering the driver.
            # Its protected receive await owns cancellation and cleanup; this
            # context retains cleanup only for incomplete acceptance.
            self.has_disconnected = True
            await drive_session(
                resource,
                ws,
                hook_manager,
                on_policy_close=on_policy_close,
            )
