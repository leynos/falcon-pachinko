"""Router-owned receive and dispatch loop for a WebSocket session."""

from __future__ import annotations

import asyncio
import dataclasses as dc
import functools
import logging
import typing as typ

import falcon
import msgspec.json as msjson

from ._disconnect import _PeerDisconnectedError
from ._observability import _observe_operation
from .diagnostics import _type_name
from .hooks import HookContext, HookEvent

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from .hooks import HookManager
    from .protocols import WebSocketLike
    from .resource import WebSocketResource

logger = logging.getLogger(__name__)


@dc.dataclass(slots=True)
class _SessionContext:
    """Hold the resource and socket selected during connection setup."""

    resource: WebSocketResource
    websocket: WebSocketLike
    hook_manager: HookManager
    has_disconnected: bool = False

    async def disconnect(self, close_code: int) -> None:
        """Attempt resource cleanup once, including when it is cancelled."""
        if self.has_disconnected:
            return
        # A cancellation inside the callback must not trigger a second call
        # when peer cleanup switches to the server-shutdown policy.
        self.has_disconnected = True
        with _observe_operation(logger, "disconnect"):
            await self.resource.on_disconnect(self.websocket, close_code)


def _adapt_frame(media: object) -> str | bytes:
    """Convert decoded Falcon media into the dispatcher's raw frame shape."""
    if isinstance(media, str | bytes):
        return media
    return msjson.encode(media)


def _log_session_failure(context: str, error: BaseException) -> None:
    """Log safe exception metadata without reflecting caller-controlled text."""
    logger.error("%s: %s", context, _type_name(error))


async def _close_after_failure(websocket: WebSocketLike, close_code: int) -> None:
    """Attempt a policy close while preserving the original session error."""
    close_error: BaseException | None = None
    try:
        await websocket.close(close_code)
    except asyncio.CancelledError as exc:
        close_error = exc
    except Exception as exc:  # ruff: ignore[blind-except] - preserve session errors
        close_error = exc
    if close_error is not None:
        _log_session_failure(
            f"Failed to close WebSocket with code {close_code}", close_error
        )


async def _attempt_cleanup(
    operation: str,
    cleanup: cabc.Callable[[], cabc.Awaitable[None]],
    *,
    suppress_errors: bool,
) -> Exception | None:
    """Run one disconnect step and return its failure when not suppressed."""
    try:
        await cleanup()
    except asyncio.CancelledError as exc:
        if not suppress_errors:
            raise
        _log_session_failure(f"Failure in {operation} cleanup", exc)
    except Exception as exc:  # ruff: ignore[blind-except] - handle all cleanup failures
        if not suppress_errors:
            return exc
        _log_session_failure(f"Failure in {operation} cleanup", exc)
    return None


async def _run_disconnect_lifecycle(
    session: _SessionContext,
    close_code: int,
    *,
    suppress_errors: bool,
) -> None:
    """Run before-disconnect hooks, then resource cleanup, in that order."""

    async def notify_hooks() -> None:
        """Notify the selected resource's disconnect hooks before cleanup."""
        await session.hook_manager.notify_before_disconnect(
            HookContext(
                event=HookEvent.BEFORE_DISCONNECT,
                target=session.resource,
                resource=None,
                ws=session.websocket,
                close_code=close_code,
            )
        )

    hook_error = await _attempt_cleanup(
        "before_disconnect", notify_hooks, suppress_errors=suppress_errors
    )

    resource_error = await _attempt_cleanup(
        "on_disconnect",
        functools.partial(session.disconnect, close_code),
        suppress_errors=suppress_errors,
    )
    if hook_error is not None:
        raise hook_error
    if resource_error is not None:
        raise resource_error


async def _complete_peer_disconnect(
    session: _SessionContext,
    close_code: int,
    on_policy_close: cabc.Callable[[], None],
) -> None:
    """Finish peer cleanup, switching to cancellation policy if interrupted."""
    try:
        await _run_disconnect_lifecycle(session, close_code, suppress_errors=False)
    except asyncio.CancelledError:
        # A peer close can race with server shutdown while lifecycle callbacks
        # are awaiting. Switch to the cancellation policy and still attempt
        # resource cleanup if a hook was interrupted.
        on_policy_close()
        await _run_disconnect_lifecycle(session, 1006, suppress_errors=True)
        raise


async def drive_session(
    resource: WebSocketResource,
    ws: WebSocketLike,
    hook_manager: HookManager,
    *,
    on_policy_close: cabc.Callable[[], None],
) -> None:
    """Receive and dispatch frames inline until the WebSocket session ends.

    Peer disconnection runs the disconnect lifecycle with the peer's close
    code (or 1006 if Falcon does not provide one). Cancellation reports 1006 to
    cleanup without sending that reserved WebSocket close code, then propagates.
    Any other receive or handler failure closes with 1011, runs best-effort
    cleanup, and is re-raised unchanged.

    Parameters
    ----------
    resource : WebSocketResource
        Resource that dispatches received frames and handles disconnect cleanup.
    ws : WebSocketLike
        Accepted socket used to receive frames and apply close policy.
    hook_manager : HookManager
        Hook manager that runs before-disconnect hooks before resource cleanup.
    on_policy_close : collections.abc.Callable[[], None]
        Callback that marks the router's close policy as attempted, preventing
        the router from closing the socket a second time.

    Raises
    ------
    asyncio.CancelledError
        After best-effort disconnect cleanup.
    """
    session = _SessionContext(resource, ws, hook_manager)
    close_code = 1000
    try:
        while True:
            try:
                media = await session.websocket.receive_media()
                raw = _adapt_frame(media)
                await session.resource.dispatch(session.websocket, raw)
            except (falcon.WebSocketDisconnected, _PeerDisconnectedError) as exc:
                close_code = exc.code if exc.code is not None else 1006
                break
    except asyncio.CancelledError:
        on_policy_close()
        await _run_disconnect_lifecycle(session, 1006, suppress_errors=True)
        raise
    except Exception as exc:
        # Record the attempt first so the router wrapper never closes twice.
        on_policy_close()
        _log_session_failure("WebSocket session failed", exc)
        await _close_after_failure(session.websocket, 1011)
        await _run_disconnect_lifecycle(session, 1011, suppress_errors=True)
        raise

    await _complete_peer_disconnect(session, close_code, on_policy_close)
