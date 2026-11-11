"""Background workers used by the reference application."""

from __future__ import annotations

import asyncio
import dataclasses as dc
import logging
import math
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


@dc.dataclass(frozen=True, slots=True)
class _DeliveryPolicy:
    """Resolved delivery and supervision options for the announcement helpers.

    Bundling the three options keeps the helper signatures small and ensures
    the consumer and the supervising loop observe the same values.
    """

    send_timeout: float
    restart_delay: float
    sleep: cabc.Callable[[float], cabc.Awaitable[None]] = asyncio.sleep


def _validate_delivery_policy(send_timeout: float, restart_delay: float) -> None:
    """Reject policy values that would silently disable delivery or supervision.

    Raises
    ------
    ValueError
        If ``send_timeout`` is not a positive number, or ``restart_delay`` is
        negative.
    """
    # NaN is rejected explicitly: every comparison against it is false, so it
    # would make every send time out without ever being reported.
    if math.isnan(send_timeout) or send_timeout <= 0:
        msg = f"send_timeout must be positive, got {send_timeout!r}"
        raise ValueError(msg)
    if restart_delay < 0:
        msg = f"restart_delay must not be negative, got {restart_delay!r}"
        raise ValueError(msg)


async def _deliver_announcement(
    conn_mgr: WebSocketConnectionManager,
    workspace_id: str,
    payload: dict[str, object],
    policy: _DeliveryPolicy,
) -> None:
    """Deliver one announcement, absorbing expected recipient failures.

    ``broadcast_to_room`` re-raises a lone recipient failure directly and
    groups two or more in an ``ExceptionGroup``; separate ``except*`` clauses
    handle both shapes and keep disconnects distinguishable from timeouts. Any
    other failure, including a non-expected member of a mixed group, still
    propagates to the supervising loop.

    The room is resolved before broadcasting so a backend failure while listing
    recipients is reported by the supervising loop instead of being mistaken
    for a recipient that stopped accepting frames.
    """
    room = _workspace_room(workspace_id)
    if not [websocket async for websocket in conn_mgr.connections(room=room)]:
        return
    try:
        await conn_mgr.broadcast_to_room(room, payload, timeout=policy.send_timeout)
    except* falcon.WebSocketDisconnected as disconnected:
        # broadcast_to_room has already attempted every recipient by the time
        # it re-raises. The count is logged without the workspace identifier so
        # retained logs cannot identify a customer.
        logger.debug(
            "Skipped %d disconnected recipient(s)",
            len(disconnected.exceptions),
        )
    except* TimeoutError as unresponsive:
        logger.debug(
            "Skipped %d unresponsive recipient(s)",
            len(unresponsive.exceptions),
        )


async def _consume_announcements(
    *,
    conn_mgr: WebSocketConnectionManager,
    announcement_feed: AnnouncementFeed,
    policy: _DeliveryPolicy,
) -> None:
    """Broadcast queued announcements until the worker is cancelled."""
    while True:
        workspace_id, payload = await announcement_feed.next_event()
        await _deliver_announcement(
            conn_mgr,
            workspace_id,
            payload,
            policy,
        )


async def _run_announcement_worker(
    *,
    conn_mgr: WebSocketConnectionManager,
    announcement_feed: AnnouncementFeed,
    policy: _DeliveryPolicy,
) -> None:
    """Consume announcements, restarting the loop after unexpected failures."""
    while True:
        try:
            await _consume_announcements(
                conn_mgr=conn_mgr,
                announcement_feed=announcement_feed,
                policy=policy,
            )
        except Exception:
            # This consumer serves every workspace, so any escaping delivery
            # failure would permanently stop unrelated users' announcements.
            logger.exception(
                "Announcement worker failed; restarting in %s seconds",
                policy.restart_delay,
            )
            await policy.sleep(policy.restart_delay)


# Explicit policy options keep the example adaptable; they are keyword-only.
# @codescene(disable:"Excess Number of Function Arguments") #270 delivery policy
@worker
# pylint: disable-next=too-many-arguments  # delivery and supervision policy is explicit
def announcement_worker(  # ruff: ignore[too-many-arguments] - delivery and supervision policy is explicit
    *,
    conn_mgr: WebSocketConnectionManager,
    announcement_feed: AnnouncementFeed,
    send_timeout: float = _SEND_TIMEOUT_SECONDS,
    restart_delay: float = _RESTART_DELAY_SECONDS,
    sleep: cabc.Callable[[float], cabc.Awaitable[None]] = asyncio.sleep,
) -> cabc.Coroutine[object, object, None]:
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

    The policy is validated before the coroutine is created, so
    ``WorkerController.start()`` raises ``ValueError`` for a non-positive or
    ``NaN`` ``send_timeout`` or a negative ``restart_delay`` instead of
    scheduling a worker that would silently skip every delivery.

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

    Returns
    -------
    collections.abc.Coroutine[object, object, None]
        The coroutine that consumes the announcement feed.
    """
    _validate_delivery_policy(send_timeout, restart_delay)
    return _run_announcement_worker(
        conn_mgr=conn_mgr,
        announcement_feed=announcement_feed,
        policy=_DeliveryPolicy(
            send_timeout=send_timeout,
            restart_delay=restart_delay,
            sleep=sleep,
        ),
    )
