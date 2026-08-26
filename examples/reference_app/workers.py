"""Background workers used by the reference application."""

from __future__ import annotations

import asyncio
import typing as typ

from falcon_pachinko import worker

from .resources import _workspace_room

if typ.TYPE_CHECKING:
    from falcon_pachinko import WebSocketConnectionManager

    from .services import AnnouncementFeed

__all__ = ["announcement_worker"]


@worker
async def announcement_worker(
    *,
    conn_mgr: WebSocketConnectionManager,
    announcement_feed: AnnouncementFeed,
) -> None:
    """Broadcast feed updates to everyone listening within a workspace."""
    try:
        while True:
            workspace_id, payload = await announcement_feed.next_event()
            await conn_mgr.broadcast_to_room(_workspace_room(workspace_id), payload)
    except asyncio.CancelledError:
        return
