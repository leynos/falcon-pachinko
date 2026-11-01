"""Background workers used by the reference application."""

from __future__ import annotations

import asyncio
import logging
import typing as typ

import falcon

from falcon_pachinko import worker

from .resources import _workspace_room

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from falcon_pachinko import WebSocketConnectionManager

    from .services import AnnouncementFeed

__all__ = ["announcement_worker"]

logger = logging.getLogger(__name__)

#: Maximum time to wait for one recipient's send. A socket that cannot accept
#: an announcement within this window is treated as a delivery failure, so one
#: unresponsive recipient cannot stall the consumer shared by every workspace.
_SEND_TIMEOUT_SECONDS = 5.0

#: Delay before the consuming loop restarts after an unexpected failure. Long
#: enough to stop a persistently failing loop from spinning, short enough that
#: a transient fault does not visibly interrupt announcements.
_RESTART_DELAY_SECONDS = 1.0


async def _deliver_announcement(
    conn_mgr: WebSocketConnectionManager,
    workspace_id: str,
    payload: dict[str, object],
    send_timeout: float,
) -> None:
    """Deliver one announcement, absorbing expected recipient failures.

    ``broadcast_to_room`` re-raises a lone recipient failure directly and
    groups two or more in an ``ExceptionGroup``; ``except*`` handles both
    shapes. Any other failure, including a non-expected member of a mixed
    group, still propagates to the supervising loop.
    """
    try:
        await conn_mgr.broadcast_to_room(
            _workspace_room(workspace_id),
            payload,
            timeout=send_timeout,
        )
    except* (falcon.WebSocketDisconnected, TimeoutError) as recipient_group:
        # A disconnected or unresponsive recipient does not fail the
        # announcement for the rest of the room: broadcast_to_room has already
        # attempted every recipient by the time it re-raises. The count is
        # logged without the workspace identifier so retained logs cannot
        # identify a customer.
        logger.debug(
            "Skipped %d disconnected or unresponsive recipient(s)",
            len(recipient_group.exceptions),
        )


async def _consume_announcements(
    *,
    conn_mgr: WebSocketConnectionManager,
    announcement_feed: AnnouncementFeed,
    send_timeout: float,
) -> None:
    """Broadcast queued announcements until the worker is cancelled."""
    while True:
        workspace_id, payload = await announcement_feed.next_event()
        await _deliver_announcement(
            conn_mgr,
            workspace_id,
            payload,
            send_timeout,
        )


# Explicit policy options keep the example adaptable; they are keyword-only.
# @codescene(disable:"Excess Number of Function Arguments") #270 delivery policy
@worker
# pylint: disable-next=too-many-arguments  # delivery and supervision policy is explicit
async def announcement_worker(  # ruff: ignore[too-many-arguments] - delivery and supervision policy is explicit
    *,
    conn_mgr: WebSocketConnectionManager,
    announcement_feed: AnnouncementFeed,
    send_timeout: float = _SEND_TIMEOUT_SECONDS,
    restart_delay: float = _RESTART_DELAY_SECONDS,
    sleep: cabc.Callable[[float], cabc.Awaitable[None]] = asyncio.sleep,
) -> None:
    """Broadcast feed updates to everyone listening within a workspace.

    Delivery is best effort: a recipient that has disconnected, or that does
    not accept an announcement within ``send_timeout`` seconds, is skipped and
    the remaining recipients still receive the announcement. Unexpected
    failures are logged with a traceback and the consuming loop is restarted
    after ``restart_delay`` seconds.

    An announcement that was dequeued when an unexpected failure occurred is
    dropped rather than requeued, so recovery does not replay it. Cancellation
    (and therefore lifespan shutdown through ``WorkerController.stop()``)
    propagates without being treated as a failure.

    Parameters
    ----------
    conn_mgr : WebSocketConnectionManager
        Connection manager that owns the workspace rooms.
    announcement_feed : AnnouncementFeed
        Feed supplying ``(workspace_id, payload)`` announcements in order.
    send_timeout : float, optional
        Maximum seconds to await each recipient's ``send_media`` call. A
        recipient that exceeds it is skipped as a delivery failure.
    restart_delay : float, optional
        Seconds to await before restarting the consuming loop after an
        unexpected failure.
    sleep : Callable[[float], Awaitable[None]], optional
        Awaitable sleep function used for ``restart_delay``. Injecting it lets
        tests observe the delay without waiting for it.
    """
    while True:
        try:
            await _consume_announcements(
                conn_mgr=conn_mgr,
                announcement_feed=announcement_feed,
                send_timeout=send_timeout,
            )
        except Exception:
            # This consumer serves every workspace, so any escaping delivery
            # failure would permanently stop unrelated users' announcements.
            logger.exception(
                "Announcement worker failed; restarting in %s seconds",
                restart_delay,
            )
            await sleep(restart_delay)
