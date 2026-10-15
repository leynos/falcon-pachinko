"""Own the opt-in WebSocket acceptance phase and transfer accepted sessions.

``WebSocketRouter(receive_loop=True)`` enters this owner after ``on_connect``
allows the connection and before acceptance completes. If acceptance setup
fails, it runs disconnect hooks and attempts resource cleanup with close code
1006. Once acceptance succeeds, it transfers frame reception and disconnect
cleanup to the shared ``session.drive_session`` loop. During cleanup, hooks
precede the resource callback, which is attempted once even if cancellation
interrupts it. The shared driver may retry a hook interrupted by cancellation
before the callback, while preventing a second callback attempt.
"""

from __future__ import annotations

import logging
import typing as typ

from ._observability import _observe_operation
from .diagnostics import _type_name
from .session import drive_session

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
    ) -> None:
        """Protect acceptance, then transfer the session to the shared driver."""
        async with self:
            await acceptance
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
