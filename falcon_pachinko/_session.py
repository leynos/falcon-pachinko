"""Internal state and failure policy for one accepted WebSocket session."""

from __future__ import annotations

import asyncio
import dataclasses as dc
import enum

GOING_AWAY = 1001
ABNORMAL_CLOSURE = 1006
INTERNAL_ERROR = 1011


async def _wait_for_shielded_task[T](
    task: asyncio.Future[T],
) -> list[asyncio.CancelledError]:
    """Wait for task completion while retaining each caller cancellation."""
    interruptions: list[asyncio.CancelledError] = []
    while True:
        try:
            await asyncio.shield(task)
            break
        except asyncio.CancelledError as exc:
            interruptions.append(exc)
            if task.done():
                break
    return interruptions


def _report_session_failure(
    error: BaseException,
    message: str,
    *,
    suppress_handler_errors: bool = False,
) -> None:
    """Report a failure without changing the lifecycle's precedence policy."""
    try:
        asyncio.get_running_loop().call_exception_handler({
            "message": message,
            "exception": error,
        })
    except Exception:
        if not suppress_handler_errors:
            raise


class SessionPhase(enum.StrEnum):
    """Phases in the lifetime of a routed WebSocket session."""

    CONNECTING = "connecting"
    ESTABLISHED = "established"
    DISCONNECTING = "disconnecting"
    CLOSED = "closed"


@dc.dataclass(slots=True)
class SessionState:
    """Mutable lifecycle state shared by the router and bound resource."""

    phase: SessionPhase = SessionPhase.CONNECTING
    close_code: int | None = None
    primary_error: BaseException | None = None

    def establish(self) -> None:
        """Record that this connection has crossed the cleanup promise point."""
        if self.phase is SessionPhase.CONNECTING:
            self.phase = SessionPhase.ESTABLISHED

    def begin_disconnect(self) -> bool:
        """Enter disconnect once, returning whether this caller owns cleanup."""
        if self.phase is not SessionPhase.ESTABLISHED:
            return False
        self.phase = SessionPhase.DISCONNECTING
        return True

    def mark_closed(self) -> None:
        """Complete the session after all resource cleanup has been attempted."""
        self.phase = SessionPhase.CLOSED

    @property
    def can_receive(self) -> bool:
        """Whether router or resource dispatch may start a receive cycle."""
        return self.phase is SessionPhase.ESTABLISHED


def effective_close_code(
    recorded_code: object,
    session: SessionState,
    primary_error: BaseException | None,
) -> int:
    """Return the first observed code or the fallback for the session outcome."""
    if isinstance(recorded_code, int):
        return recorded_code
    if session.close_code is not None:
        return session.close_code
    match primary_error:
        case asyncio.CancelledError():
            return GOING_AWAY
        case None:
            return ABNORMAL_CLOSURE
        case _:
            return INTERNAL_ERROR
