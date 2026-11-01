"""Reference example wiring showcasing the advanced feature set."""

# /// script
# dependencies = [
#     "falcon",
#     "falcon-pachinko",
#     "msgspec",
#     "uvicorn",
# ]
# ///

from __future__ import annotations

import typing as typ

import falcon
import falcon.asgi

from falcon_pachinko import (
    ServiceContainer,
    WebSocketConnectionManager,
    WebSocketResource,
    WebSocketRouter,
    WorkerController,
    install,
)
from falcon_pachinko.hooks import HookContext, HookEvent

from .resources import WorkspaceResource, register_reference_hooks
from .services import (
    AnnouncementFeed,
    AuditTrail,
    AuthenticationError,
    TokenAuthenticator,
    WorkspaceRepository,
)
from .workers import announcement_worker

if typ.TYPE_CHECKING:  # pragma: no cover - typing helpers
    import collections.abc as cabc

    from falcon_pachinko.router import SimulatorFactory


class _WorkerLifecycleMiddleware:
    """Bind the announcement worker to Falcon's ASGI lifespan."""

    def __init__(
        self,
        controller: WorkerController,
        conn_mgr: WebSocketConnectionManager,
        feed: AnnouncementFeed,
    ) -> None:
        self._controller = controller
        self._conn_mgr = conn_mgr
        self._feed = feed

    async def process_startup(
        self, scope: cabc.Mapping[str, typ.Any], event: cabc.Mapping[str, typ.Any]
    ) -> None:
        """Start the feed consumer after Falcon receives lifespan startup."""
        del scope, event
        await self._controller.start(
            announcement_worker,
            conn_mgr=self._conn_mgr,
            announcement_feed=self._feed,
        )

    async def process_shutdown(
        self, scope: cabc.Mapping[str, typ.Any], event: cabc.Mapping[str, typ.Any]
    ) -> None:
        """Stop the feed consumer before Falcon finishes lifespan shutdown."""
        del scope, event
        await self._controller.stop()


class ReferenceApp(falcon.asgi.App):
    """Falcon ASGI app with one slot for the example's service container."""

    __slots__ = ("reference_services",)
    reference_services: ServiceContainer


def _require_token_hook(
    authenticator: TokenAuthenticator, audit: AuditTrail
) -> cabc.Callable[[HookContext], cabc.Awaitable[None]]:
    """Return a ``before_connect`` hook that refuses unauthenticated connections.

    Each refusal is audited as ``auth.rejected`` with the failure's bounded
    ``reason`` (``unknown_workspace`` or ``invalid_token``) before the 401 is
    raised, so rejections can be counted and told apart the same way accepted
    sessions are. The record never carries the presented token. A rejected
    known workspace is named; an unknown one is not, because its identifier
    is whatever the caller put in the path.

    Returns
    -------
    Callable[[HookContext], Awaitable[None]]
        The hook to register for ``HookEvent.BEFORE_CONNECT``.
    """

    async def _hook(context: HookContext) -> None:
        params = context.params or {}
        raw_workspace_id = params.get("workspace_id")
        workspace_id = raw_workspace_id if isinstance(raw_workspace_id, str) else None
        token = context.req.get_header("x-workspace-token") if context.req else None
        try:
            await authenticator.verify(workspace_id or "default", token)
        except AuthenticationError as exc:
            if exc.reason == "invalid_token":
                await audit.record(
                    "auth.rejected", reason=exc.reason, workspace=exc.workspace_id
                )
            else:
                await audit.record("auth.rejected", reason=exc.reason)
            raise falcon.HTTPUnauthorized(description=str(exc)) from exc

    return _hook


def _resolve_as[T](container: ServiceContainer, name: str, expected: type[T]) -> T:
    """Resolve ``name`` from ``container``, validating its concrete type."""
    value = container.resolve(name)
    if not isinstance(value, expected):
        msg = f"service {name!r} is not a {expected.__name__}"
        raise TypeError(msg)
    return value


def build_container(conn_mgr: WebSocketConnectionManager) -> ServiceContainer:
    """Create and populate the service container used for DI."""
    container = ServiceContainer()
    repo = WorkspaceRepository()
    audit = AuditTrail()
    feed = AnnouncementFeed()
    authenticator = TokenAuthenticator({"atlas": "seekrit", "zephyr": "seekrit"})
    container.register("workspace_repo", repo)
    container.register("audit_trail", audit)
    container.register("announcement_feed", feed)
    container.register("conn_mgr", conn_mgr)
    container.register("token_authenticator", authenticator)
    return container


def build_router(
    container: ServiceContainer,
    *,
    simulator_factory: SimulatorFactory | None = None,
    resource_factory: cabc.Callable[
        [cabc.Callable[..., WebSocketResource]], WebSocketResource
    ]
    | None = None,
) -> WebSocketRouter:
    """Construct the router with routes, hooks, and DI wiring."""
    register_reference_hooks()
    router = WebSocketRouter(
        name="reference",
        resource_factory=resource_factory or container.create_resource,
        simulator_factory=simulator_factory,
        receive_loop=True,
    )
    router.add_route(
        "/workspaces/{workspace_id}",
        WorkspaceResource,
        name="workspace",
    )
    router.mount("/ws")

    authenticator = _resolve_as(container, "token_authenticator", TokenAuthenticator)
    audit = _resolve_as(container, "audit_trail", AuditTrail)
    router.global_hooks.add(
        HookEvent.BEFORE_CONNECT, _require_token_hook(authenticator, audit)
    )
    return router


def create_app() -> ReferenceApp:
    """Create the reference ASGI app.

    The service container is exposed as ``app.reference_services`` for
    inspection of the example's audit trail, workspace repository, and
    connection manager.

    Returns
    -------
    ReferenceApp
        The Falcon application with the reference router and worker installed.
    """
    app = ReferenceApp()
    conn_mgr = install(app)
    container = build_container(conn_mgr)
    router = build_router(container)
    app.reference_services = container
    controller = WorkerController()
    feed = _resolve_as(container, "announcement_feed", AnnouncementFeed)
    app.add_middleware(_WorkerLifecycleMiddleware(controller, conn_mgr, feed))
    router.attach(app)

    return app


if __name__ == "__main__":  # pragma: no cover - manual execution helper
    import uvicorn

    uvicorn.run(create_app(), host="127.0.0.1", port=8000)
