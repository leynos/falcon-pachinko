"""Router-owned receive and dispatch loop for a WebSocket session."""

from __future__ import annotations

import asyncio
import dataclasses as dc
import logging
import typing as typ

import falcon
import msgspec.json as msjson

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from .hooks import HookManager
    from .protocols import WebSocketLike
    from .resource import WebSocketResource

logger = logging.getLogger(__name__)


@dc.dataclass(frozen=True, slots=True)
class _SessionContext:
    """Hold the resource and socket selected during connection setup."""

    resource: WebSocketResource
    websocket: WebSocketLike
    hook_manager: HookManager


def _adapt_frame(media: object) -> str | bytes:
    """Convert decoded Falcon media into the dispatcher's raw frame shape."""
    if isinstance(media, str | bytes):
        return media
    return msjson.encode(media)


async def _close_after_failure(websocket: WebSocketLike, close_code: int) -> None:
    """Attempt a policy close while preserving the original session error."""
    try:
        await websocket.close(close_code)
    except (Exception, asyncio.CancelledError):
        logger.exception("Failed to close WebSocket with code %s", close_code)


async def _attempt_cleanup(
    operation: str,
    cleanup: cabc.Callable[[], cabc.Awaitable[None]],
    *,
    suppress_errors: bool,
) -> Exception | None:
    """Run one disconnect step and return its failure when not suppressed."""
    try:
        await cleanup()
    except asyncio.CancelledError:
        if not suppress_errors:
            raise
        logger.exception("Failure in %s cleanup", operation)
    except Exception as exc:
        if not suppress_errors:
            return exc
        logger.exception("Failure in %s cleanup", operation)
    return None


async def _run_disconnect_lifecycle(
    session: _SessionContext,
    close_code: int,
    *,
    suppress_errors: bool,
) -> None:
    """Run before-disconnect hooks, then resource cleanup, in that order."""

    async def notify_hooks() -> None:
        await session.hook_manager.notify_before_disconnect(
            session.resource,
            ws=session.websocket,
            close_code=close_code,
        )

    hook_error = await _attempt_cleanup(
        "before_disconnect", notify_hooks, suppress_errors=suppress_errors
    )

    async def disconnect_resource() -> None:
        await session.resource.on_disconnect(session.websocket, close_code)

    resource_error = await _attempt_cleanup(
        "on_disconnect", disconnect_resource, suppress_errors=suppress_errors
    )
    if hook_error is not None:
        raise hook_error
    if resource_error is not None:
        raise resource_error


async def drive_session(
    resource: WebSocketResource,
    ws: WebSocketLike,
    hook_manager: HookManager,
) -> None:
    """Receive and dispatch frames inline until the WebSocket session ends.

    Peer disconnection runs the disconnect lifecycle with the peer's close
    code (or 1000 if Falcon does not provide one). Cancellation closes with
    1001 and is re-raised. Any other receive or handler failure closes with
    1011, runs best-effort cleanup, and is re-raised unchanged.

    Raises
    ------
    asyncio.CancelledError
        After best-effort closure and disconnect cleanup.
    """
    session = _SessionContext(resource, ws, hook_manager)
    close_code = 1000
    try:
        while True:
            try:
                media = await session.websocket.receive_media()
                raw = _adapt_frame(media)
                await session.resource.dispatch(session.websocket, raw)
            except falcon.WebSocketDisconnected as exc:
                close_code = exc.code if exc.code is not None else 1000
                break
    except asyncio.CancelledError:
        await _close_after_failure(session.websocket, 1001)
        await _run_disconnect_lifecycle(session, 1001, suppress_errors=True)
        raise
    except Exception as exc:
        # The outer wrapper must not close the socket after this policy close.
        typ.cast("typ.Any", exc)._pachinko_factory_closed = True
        await _close_after_failure(session.websocket, 1011)
        await _run_disconnect_lifecycle(session, 1011, suppress_errors=True)
        raise

    await _run_disconnect_lifecycle(session, close_code, suppress_errors=False)
