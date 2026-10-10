"""Persistent WebSocket session receiving and cleanup lifecycle."""

from __future__ import annotations

import asyncio
import typing as typ

import falcon

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from .protocols import WebSocketLike
    from .resource import WebSocketResource

_WS_NORMAL_CLOSURE = 1000
_WS_ABNORMAL_CLOSURE = 1006
_WS_INTERNAL_ERROR = 1011


def _is_socket_closed(ws: WebSocketLike) -> bool:
    """Return whether Falcon or a compatible socket has already closed."""
    return bool(getattr(ws, "closed", False))


async def _receive_raw_frame(ws: WebSocketLike) -> str | bytes:
    """Receive one raw frame or explain how to configure the app."""
    raw = await ws.receive_media()
    if not isinstance(raw, str | bytes):
        msg = (
            "receive_media() must return a raw str or bytes frame; "
            "install the router with WebSocketRouter.attach(app, prefix) "
            "to configure pass-through Falcon WebSocket media handlers"
        )
        raise TypeError(msg)
    return raw


async def _receive_session(resource: WebSocketResource, ws: WebSocketLike) -> int:
    """Dispatch each raw frame until Falcon reports a disconnect."""
    while True:
        try:
            raw = await _receive_raw_frame(ws)
            await resource.dispatch(ws, raw)
        except falcon.WebSocketDisconnected as exc:
            return exc.code if exc.code is not None else _WS_NORMAL_CLOSURE


async def _notify_disconnect(
    resource: WebSocketResource, ws: WebSocketLike, close_code: int
) -> None:
    """Run registered before-disconnect hooks for this resource."""
    manager = resource._require_hook_manager()
    await manager.notify_before_disconnect(resource, ws=ws, close_code=close_code)


async def _capture_cleanup_error(
    action: cabc.Awaitable[object], errors: list[Exception]
) -> None:
    """Append cleanup failures without replacing the primary failure."""
    try:
        await action
    # Preserve arbitrary cleanup failures so they cannot mask the primary.
    except Exception as exc:  # ruff: ignore[blind-except]
        errors.append(exc)


async def _finalize_session(
    resource: WebSocketResource, ws: WebSocketLike, close_code: int
) -> list[Exception]:
    """Close failed sessions and run disconnect hooks once."""
    cleanup_errors: list[Exception] = []
    if close_code == _WS_INTERNAL_ERROR and not _is_socket_closed(ws):
        await _capture_cleanup_error(ws.close(code=_WS_INTERNAL_ERROR), cleanup_errors)

    await _capture_cleanup_error(
        _notify_disconnect(resource, ws, close_code), cleanup_errors
    )
    await _capture_cleanup_error(resource.on_disconnect(ws, close_code), cleanup_errors)
    return cleanup_errors


async def run_session(resource: WebSocketResource, ws: WebSocketLike) -> None:
    """Receive and dispatch frames, then clean the connection exactly once.

    A clean Falcon disconnect preserves its close code. Dispatch failures
    close with 1011; cancellation reports 1006 to cleanup because that
    reserved code describes an abnormal local termination and is never
    sent in a WebSocket close frame.
    """
    close_code = _WS_NORMAL_CLOSURE
    primary_error: BaseException | None = None
    try:
        close_code = await _receive_session(resource, ws)
    except asyncio.CancelledError as exc:
        primary_error = exc
        close_code = _WS_ABNORMAL_CLOSURE
    # The responder must close on every ordinary dispatch failure.
    except Exception as exc:  # ruff: ignore[blind-except]
        primary_error = exc
        close_code = _WS_INTERNAL_ERROR
    finally:
        cleanup_errors = await _finalize_session(resource, ws, close_code)

    if primary_error is not None:
        for cleanup_error in cleanup_errors:
            primary_error.add_note(
                f"WebSocket session cleanup also failed: {cleanup_error!r}"
            )
        raise primary_error
    if cleanup_errors:
        first_error = cleanup_errors[0]
        for cleanup_error in cleanup_errors[1:]:
            first_error.add_note(
                f"Additional WebSocket cleanup failure: {cleanup_error!r}"
            )
        raise first_error
