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
    WebSocketConnectionManager,
    WebSocketLike,
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

try:  # pragma: no cover - used when tests are available
    from tests.behaviour._lifespan import LifespanApp
except ImportError:  # pragma: no cover - fallback when running the script

    class LifespanApp(falcon_asgi.App):
        """Falcon App variant exposing ``@app.lifespan`` for uvicorn."""

        def __init__(self) -> None:
            super().__init__()
            self._lifespan_handler: (
                cabc.Callable[[LifespanApp], cl.AbstractAsyncContextManager[None]]
                | None
            ) = None

        def lifespan(
            self, fn: cabc.Callable[[LifespanApp], cabc.AsyncIterator[None]]
        ) -> cabc.Callable[[LifespanApp], cl.AbstractAsyncContextManager[None]]:
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

    class _PachinkoApp(typ.Protocol):
        """Attributes attached to the app at runtime by :func:`install`."""

        ws_connection_manager: WebSocketConnectionManager

        def add_websocket_route(
            self,
            uri_template: str,
            resource: type[WebSocketResource] | cabc.Callable[..., WebSocketResource],
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
) -> cabc.Callable[[HookContext], cabc.Awaitable[None]]:
    async def _hook(context: HookContext) -> None:
        params = context.params or {}
        raw_workspace_id = params.get("workspace_id")
        workspace_id = raw_workspace_id if isinstance(raw_workspace_id, str) else None
        token = context.req.get_header("x-workspace-token") if context.req else None
        try:
            await authenticator.verify(workspace_id or "default", token)
        except AuthenticationError as exc:
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
    )
    router.add_route(
        "/workspaces/{workspace_id}",
        WorkspaceResource,
        name="workspace",
    )
    router.mount("/ws")

    authenticator = _resolve_as(container, "token_authenticator", TokenAuthenticator)
    router.global_hooks.add(
        HookEvent.BEFORE_CONNECT, _require_token_hook(authenticator)
    )
    return router


def create_app() -> LifespanApp:
    """Create the Falcon ASGI app with the full reference configuration."""
    app = LifespanApp()
    install(app)
    # Cast: install() attaches these attributes at runtime, so the type
    # checker cannot see them on falcon.asgi.App.
    ws_app = typ.cast("_PachinkoApp", app)
    conn_mgr = ws_app.ws_connection_manager
    container = build_container(conn_mgr)
    router = build_router(container)

    ws_app.add_websocket_route("/ws", RouterEndpoint, router=router)

    controller = WorkerController()
    feed = _resolve_as(container, "announcement_feed", AnnouncementFeed)

    @app.lifespan
    async def lifespan(_app: LifespanApp) -> cabc.AsyncIterator[None]:
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
