"""Track phases and failure state for one routed WebSocket session.

The session driver shares this state with the socket adapter and cleanup task.
It records the effective close code and primary error, while
``begin_disconnect()`` grants exactly one caller cleanup ownership.
"""

from __future__ import annotations

import dataclasses as dc
import enum

GOING_AWAY = 1001
ABNORMAL_CLOSURE = 1006
INTERNAL_ERROR = 1011
MAX_CLOSE_CODE = 65535


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
    _has_established: bool = dc.field(default=False, init=False, repr=False)

    def establish(self) -> None:
        """Record that this connection has crossed the cleanup promise point."""
        if self.phase is SessionPhase.CONNECTING:
            self._has_established = True
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
    def has_established(self) -> bool:
        """Whether this session ever crossed its cleanup-promise point."""
        return self._has_established

    @property
    def can_receive(self) -> bool:
        """Whether router or resource dispatch may start a receive cycle."""
        return self.phase is SessionPhase.ESTABLISHED
