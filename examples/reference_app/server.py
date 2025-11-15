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

import contextlib as cl
import typing as typ

import falcon
import falcon.asgi as falcon_asgi

from falcon_pachinko import (
    ServiceContainer,
    WebSocketResource,
    WebSocketRouter,
    WorkerController,
    install,
)
from falcon_pachinko.hooks import HookContext, HookEvent

from .resources import WorkspaceResource
from .services import (
    AnnouncementFeed,
    AuditTrail,
    AuthenticationError,
    TokenAuthenticator,
    WorkspaceRepository,
)
from .workers import announcement_worker

try:  # pragma: no cover - used when tests are available
    from tests.behaviour._lifespan import LifespanApp
except ImportError:  # pragma: no cover - fallback when running the script

    class LifespanApp(falcon_asgi.App):
        """Falcon App variant exposing ``@app.lifespan`` for uvicorn."""

        def __init__(self) -> None:
            super().__init__()
            self._lifespan_handler: (
                typ.Callable[[LifespanApp], cl.AbstractAsyncContextManager[None]] | None
            ) = None

        def lifespan(  # type: ignore[override]
            self, fn: typ.Callable[[LifespanApp], typ.AsyncIterator[None]]
        ) -> typ.Callable[[LifespanApp], cl.AbstractAsyncContextManager[None]]:
            """Register ``fn`` as the lifespan context manager."""
            manager = cl.asynccontextmanager(fn)
            self._lifespan_handler = manager
            return manager

        def lifespan_context(self) -> cl.AbstractAsyncContextManager[None]:
            """Return the active lifespan context manager."""
            if self._lifespan_handler is None:
                msg = "lifespan handler not set"
                raise RuntimeError(msg)
            return self._lifespan_handler(self)


if typ.TYPE_CHECKING:  # pragma: no cover - typing helpers
    from falcon_pachinko.protocols import WebSocketLike
    from falcon_pachinko.router import SimulatorFactory
    from falcon_pachinko.websocket import WebSocketConnectionManager
else:  # pragma: no cover - runtime aliases for annotations
    WebSocketLike = typ.Any  # type: ignore[assignment]
    SimulatorFactory = typ.Any  # type: ignore[assignment]
    WebSocketConnectionManager = typ.Any  # type: ignore[assignment]

    class _SupportsWebSocketRoute(typ.Protocol):
        def add_websocket_route(
            self,
            uri_template: str,
            resource: type[WebSocketResource] | typ.Callable[..., WebSocketResource],
            *args: object,
            **kwargs: object,
        ) -> None: ...


class RouterEndpoint(WebSocketResource):
    """Adapter that wires a :class:`WebSocketRouter` into the app."""

    def __init__(self, *, router: WebSocketRouter) -> None:
        self._router = router

    async def on_connect(
        self, req: falcon.Request, ws: WebSocketLike, **params: object
    ) -> bool:
        """Delegate the connection lifecycle to the router."""
        await self._router.on_websocket(req, ws)
        return False


def _require_token_hook(
    authenticator: TokenAuthenticator,
) -> typ.Callable[[HookContext], typ.Awaitable[None]]:
    async def _hook(context: HookContext) -> None:
        params = context.params or {}
        workspace_id = typ.cast("str | None", params.get("workspace_id"))
        token = context.req.get_header("x-workspace-token") if context.req else None
        try:
            await authenticator.verify(workspace_id or "default", token)
        except AuthenticationError as exc:
            raise falcon.HTTPUnauthorized(description=str(exc)) from exc

    return _hook


def _inject_user_param() -> typ.Callable[[HookContext], typ.Awaitable[None] | None]:
    async def _hook(context: HookContext) -> None:
        if context.req is None:
            return
        user = context.req.get_header("x-user", default="guest")
        params = context.params or {}
        params.setdefault("user", user)
        context.params = params

    return _hook


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
    resource_factory: typ.Callable[
        [typ.Callable[..., WebSocketResource]], WebSocketResource
    ]
    | None = None,
) -> WebSocketRouter:
    """Construct the router with routes, hooks, and DI wiring."""
    router = WebSocketRouter(
        name="reference",
        resource_factory=resource_factory or container.create_resource,
        simulator_factory=simulator_factory,
    )
    router.add_route(
        "/workspaces/{workspace_id}",
        WorkspaceResource,
        name="workspace",
    )
    router.mount("/ws")

    authenticator = typ.cast(
        "TokenAuthenticator", container.resolve("token_authenticator")
    )
    router.global_hooks.add(
        HookEvent.BEFORE_CONNECT, _require_token_hook(authenticator)
    )
    router.global_hooks.add(HookEvent.BEFORE_CONNECT, _inject_user_param())
    return router


def create_app() -> LifespanApp:
    """Create the Falcon ASGI app with the full reference configuration."""
    app = LifespanApp()
    install(app)
    conn_mgr = typ.cast(
        "WebSocketConnectionManager",
        app.ws_connection_manager,
    )
    container = build_container(conn_mgr)
    router = build_router(container)

    ws_app = typ.cast("_SupportsWebSocketRoute", app)
    ws_app.add_websocket_route("/ws", RouterEndpoint, router=router)

    controller = WorkerController()
    feed = typ.cast("AnnouncementFeed", container.resolve("announcement_feed"))

    @app.lifespan
    async def lifespan(_app: LifespanApp) -> typ.AsyncIterator[None]:
        await controller.start(
            announcement_worker,
            conn_mgr=conn_mgr,
            announcement_feed=feed,
        )
        try:
            yield
        finally:
            await controller.stop()

    return app


if __name__ == "__main__":  # pragma: no cover - manual execution helper
    import uvicorn

    uvicorn.run(create_app(), host="127.0.0.1", port=8000)
