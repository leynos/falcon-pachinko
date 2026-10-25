"""Falcon route attachment for :class:`WebSocketRouter`."""

from __future__ import annotations

import dataclasses as dc
import typing as typ

from ._uri_template import compile_uri_template
from ._ws_media import install_raw_websocket_media_handlers

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    import falcon
    import falcon.asgi

    from ._route_support import _CompiledRoute
    from .router import WebSocketRouter


@dc.dataclass(slots=True)
class _AttachmentSnapshot:
    """Capture router and Falcon media state for attachment rollback."""

    mount_prefix: str
    routes: list[_CompiledRoute]
    media_handlers: cabc.MutableMapping[falcon.WebSocketPayloadType, object]
    original_media_handlers: dict[falcon.WebSocketPayloadType, object]


def attach_router(
    router: WebSocketRouter,
    app: falcon.asgi.App,
    prefix: str | None,
) -> None:
    """Attach ``router`` to a validated Falcon app, restoring state on failure."""
    with router._mount_lock:
        if router._attached:
            msg = "router is already attached to a Falcon app"
            raise RuntimeError(msg)
        _attach_locked(router, app, prefix)


def _attach_locked(
    router: WebSocketRouter,
    app: falcon.asgi.App,
    prefix: str | None,
) -> None:
    """Attach routes while holding the router mount lock."""
    canonical = _resolve_attach_prefix(router, prefix)
    media_handlers = typ.cast(
        "cabc.MutableMapping[falcon.WebSocketPayloadType, object]",
        app.ws_options.media_handlers,
    )
    snapshot = _AttachmentSnapshot(
        mount_prefix=router._mount_prefix,
        routes=list(router._routes),
        media_handlers=media_handlers,
        original_media_handlers=dict(media_handlers),
    )
    if not snapshot.mount_prefix:
        router._mount_locked(canonical)
    try:
        _register_attached_routes(router, app, canonical, media_handlers)
    except BaseException:
        _restore_attachment(router, snapshot)
        raise
    router._attached = True


def _resolve_attach_prefix(router: WebSocketRouter, prefix: str | None) -> str:
    """Return the existing mount or validate a supplied prefix."""
    if prefix is None:
        if not router._mount_prefix:
            msg = "router must be mounted before it can be attached"
            raise RuntimeError(msg)
        return router._mount_prefix

    canonical = router._canonical_mount_prefix(prefix)
    if router._mount_prefix and canonical != router._mount_prefix:
        msg = (
            f"router is already mounted at '{router._mount_prefix}', not '{canonical}'"
        )
        raise RuntimeError(msg)
    return canonical


def _register_attached_routes(
    router: WebSocketRouter,
    app: falcon.asgi.App,
    canonical: str,
    media_handlers: cabc.MutableMapping[falcon.WebSocketPayloadType, object],
) -> None:
    """Install raw frame handlers and register the mount routes."""
    install_raw_websocket_media_handlers(media_handlers, stacklevel=6)
    prefix_template = canonical.rstrip("/") or "/"
    routes = (prefix_template, _greedy_route_template(router, canonical))
    for route in routes:
        app.add_route(route, router)


def _restore_attachment(
    router: WebSocketRouter,
    snapshot: _AttachmentSnapshot,
) -> None:
    """Restore router and shared media state after a failed registration."""
    # Media handlers are shared by every WebSocket route on the app.
    snapshot.media_handlers.clear()
    snapshot.media_handlers.update(snapshot.original_media_handlers)
    # Falcon may have accepted the first route before rejecting the second.
    # Re-adding that route on retry replaces the partial registration.
    router._mount_prefix = snapshot.mount_prefix
    router._routes[:] = snapshot.routes


def _greedy_route_template(router: WebSocketRouter, prefix: str) -> str:
    """Build the nested Falcon route with an unused capture name."""
    used_names = set(compile_uri_template(router._mount_prefix).groupindex)
    for route in router._raw:
        used_names.update(compile_uri_template(route.canonical).groupindex)

    field_name = "_falcon_pachinko_path"
    while field_name in used_names:
        field_name += "_"
    if prefix == "/":
        return f"/{{{field_name}:path}}"
    return f"{prefix}/{{{field_name}:path}}"
