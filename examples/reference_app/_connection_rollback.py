"""Preserve connection membership when reference-resource setup fails."""

from __future__ import annotations

import asyncio
import dataclasses as dc
import typing as typ

if typ.TYPE_CHECKING:
    from falcon_pachinko import WebSocketConnectionManager, WebSocketLike


class _ConnectionIdentity(typ.Protocol):
    """Expose the identity needed to remove a partially opened session."""

    conn_id: str


class _RollbackOwner(typ.Protocol):
    """Provide the reference-resource state changed by setup rollback."""

    _conn_mgr: WebSocketConnectionManager
    _conn_id: str | None

    @property
    def has_router_session(self) -> bool:
        """Whether router-owned cleanup has accepted-session ownership."""


@dc.dataclass(frozen=True, slots=True)
class _SetupRollback:
    """Keep the state needed to roll back one failed connection setup."""

    owner: _RollbackOwner
    session: _ConnectionIdentity
    websocket: WebSocketLike
    is_accepted: bool
    error: BaseException


async def _rollback_setup_without_router_cleanup(
    setup: _SetupRollback,
) -> None:
    """Remove membership when router cleanup cannot own the setup failure."""
    was_accepted = (
        setup.is_accepted or getattr(setup.websocket, "unaccepted", True) is False
    )
    # A router-owned accepted session removes membership in its promised hook.
    if setup.owner.has_router_session and was_accepted:
        return
    try:
        interruption = await _rollback_connection(setup.owner, setup.session.conn_id)
    # Backends can fail removal for any ordinary reason; retain that error.
    except Exception as removal_error:  # ruff: ignore[blind-except] - retain both
        _preserve_setup_failure(setup.error, removal_error)
        return
    if interruption is not None and not isinstance(setup.error, asyncio.CancelledError):
        raise interruption from setup.error


def _preserve_setup_failure(
    error: BaseException,
    removal_error: Exception,
) -> None:
    """Keep setup primary while exposing a failed membership rollback."""
    if isinstance(error, Exception):
        raise ExceptionGroup("rollback", [error, removal_error]) from None
    error.add_note(f"Connection rollback also failed: {removal_error!r}")


async def _rollback_connection(
    owner: _RollbackOwner, conn_id: str
) -> asyncio.CancelledError | None:
    """Join direct or unaccepted removal through repeated cancellation."""
    # Direct calls have no router-owned disconnect callback to remove the
    # connection, so rollback must finish before on_connect exits.
    removal = asyncio.create_task(owner._conn_mgr.remove_connection(conn_id))
    interruption = await _join_connection_removal(removal)
    owner._conn_id = None
    return interruption


async def _join_connection_removal(
    removal: asyncio.Task[None],
) -> asyncio.CancelledError | None:
    """Join rollback despite cancellation, preserving its first signal."""
    interruption = await _wait_for_connection_removal(removal)
    _check_connection_removal_result(removal, interruption)
    return interruption


async def _wait_for_connection_removal(
    removal: asyncio.Task[None],
) -> asyncio.CancelledError | None:
    """Shield removal from repeated cancellation of the setup task."""
    interruption: asyncio.CancelledError | None = None
    while not removal.done():
        try:
            await asyncio.shield(removal)
        except asyncio.CancelledError as exc:
            if interruption is None:
                interruption = exc
        except Exception as removal_error:  # ruff: ignore[blind-except] - preserve cancellation precedence
            _raise_removal_failure(interruption, removal_error)
    return interruption


def _check_connection_removal_result(
    removal: asyncio.Task[None],
    interruption: asyncio.CancelledError | None,
) -> None:
    """Surface removal failure without replacing an earlier cancellation."""
    try:
        removal.result()
    except Exception as removal_error:  # ruff: ignore[blind-except] - preserve cancellation precedence
        _raise_removal_failure(interruption, removal_error)


def _raise_removal_failure(
    interruption: asyncio.CancelledError | None,
    removal_error: Exception,
) -> typ.NoReturn:
    """Preserve cancellation as primary when rollback also fails."""
    if interruption is not None:
        raise interruption from removal_error
    raise removal_error
