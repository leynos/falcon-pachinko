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

#: Delay before the consuming loop restarts after an unexpected failure. Long
#: enough to stop a persistently failing loop from spinning, short enough that
#: a transient fault does not visibly interrupt announcements.
_RESTART_DELAY_SECONDS = 1.0


async def _deliver_announcement(
    conn_mgr: WebSocketConnectionManager,
    workspace_id: str,
    payload: dict[str, object],
) -> None:
    """Deliver one announcement, absorbing expected recipient disconnections.

    ``broadcast_to_room`` re-raises a lone recipient failure directly and
    groups two or more in an ``ExceptionGroup``; ``except*`` handles both
    shapes. Any other failure, including a non-disconnect member of a mixed
    group, still propagates to the supervising loop.
    """
    try:
        await conn_mgr.broadcast_to_room(_workspace_room(workspace_id), payload)
    except* falcon.WebSocketDisconnected as disconnect_group:
        # A disconnecting recipient does not fail the announcement for the
        # rest of the room: broadcast_to_room has already attempted every
        # recipient by the time it re-raises.
        logger.debug(
            "Skipped %d disconnected recipient(s) in workspace %s",
            len(disconnect_group.exceptions),
            workspace_id,
        )


async def _consume_announcements(
    *,
    conn_mgr: WebSocketConnectionManager,
    announcement_feed: AnnouncementFeed,
) -> None:
    """Broadcast queued announcements until the worker is cancelled."""
    while True:
        workspace_id, payload = await announcement_feed.next_event()
        await _deliver_announcement(conn_mgr, workspace_id, payload)


@worker
async def announcement_worker(
    *,
    conn_mgr: WebSocketConnectionManager,
    announcement_feed: AnnouncementFeed,
    restart_delay: float = _RESTART_DELAY_SECONDS,
    sleep: cabc.Callable[[float], cabc.Awaitable[None]] = asyncio.sleep,
) -> None:
    """Broadcast feed updates to everyone listening within a workspace.

    Delivery is best effort: a recipient that has already disconnected is
    skipped, and the remaining recipients still receive the announcement.
    Unexpected failures are logged with a traceback and the consuming loop is
    restarted after ``restart_delay`` seconds.

    An announcement that was dequeued when an unexpected failure occurred is
    dropped rather than requeued, so recovery does not replay it. Cancellation
    (and therefore lifespan shutdown through ``WorkerController.stop()``)
    propagates without being treated as a failure.
    """
    while True:
        try:
            await _consume_announcements(
                conn_mgr=conn_mgr,
                announcement_feed=announcement_feed,
            )
        except Exception:
            # This consumer serves every workspace, so any escaping delivery
            # failure would permanently stop unrelated users' announcements.
            logger.exception(
                "Announcement worker failed; restarting in %s seconds",
                restart_delay,
            )
            await sleep(restart_delay)
