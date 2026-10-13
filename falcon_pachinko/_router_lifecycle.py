"""Carry connection state and serve mounted WebSocket routers through Falcon.

The private dataclasses keep the resolved resource, effective socket, and hook
manager together between setup and disconnect handling. WebSocketRouter.attach
registers the responder adapter, which owns accepted text-frame reception.

Applications use these helpers indirectly by mounting and attaching a router::

    router.mount("/ws")
    router.attach(app)
"""

from __future__ import annotations

import dataclasses as dc
import typing as typ

if typ.TYPE_CHECKING:
    import falcon
    import falcon.asgi

    from .hooks import HookContext, HookManager
    from .protocols import WebSocketLike
    from .resource import WebSocketResource
    from .router import WebSocketRouter, _RouteMatch


class _RequestLike(typ.Protocol):
    """Request surface consumed by the router before resource dispatch."""

    @property
    def path(self) -> str:
        """The request path."""


def _request_path_template(req: _RequestLike) -> str:
    """Return the mount template, without an adapter's greedy path field."""
    template = getattr(req, "path_template", None)
    if template is None:
        template = getattr(req, "uri_template", "")
    if not isinstance(template, str):
        return ""

    template = template.rstrip("/")
    prefix, separator, field = template.rpartition("/")
    is_terminal_path_field = field.startswith("{") and field.endswith(":path}")
    if separator and is_terminal_path_field:
        return prefix or "/"
    return template


@dc.dataclass(slots=True)
class _AcceptedConnection:
    """The resource, effective socket, and hooks for an accepted connection."""

    resource: WebSocketResource
    ws: WebSocketLike
    hook_manager: HookManager


@dc.dataclass(slots=True)
class _RouteAttempt:
    """Distinguish a rejected route from a path that did not resolve."""

    matched: bool
    connection: _AcceptedConnection | None = None


@dc.dataclass(slots=True)
class _Dispatch:
    """Mutable per-request state threaded through the routing pipeline."""

    route: WebSocketRouter._CompiledRoute
    req: falcon.Request
    ws: WebSocketLike
    match: _RouteMatch
    accepted: bool = False


@dc.dataclass(slots=True)
class _ConnectionFinalization:
    """State needed to finish a routed connection after ``on_connect``."""

    dispatch: _Dispatch
    should_accept: bool
    context: HookContext
    hook_manager: HookManager


class _TextReceivingWebSocket(typ.Protocol):
    """Text-only receive surface required by the Falcon responder adapter."""

    async def receive_text(self) -> str:
        """Receive the next text frame from the client."""


class _FalconResponderAdapter:
    """Let Falcon route accepted sockets into the router-owned session loop."""

    def __init__(self, router: WebSocketRouter) -> None:
        self._router = router

    async def on_websocket(
        self,
        req: falcon.asgi.Request,
        ws: falcon.asgi.WebSocket,
        **params: object,
    ) -> None:
        """Set up the route and consume text frames while the socket is open."""
        del params
        connection = await self._router._setup_websocket(
            req, typ.cast("WebSocketLike", ws)
        )
        if connection is not None:
            await self._router._drive_accepted_connection(connection)
