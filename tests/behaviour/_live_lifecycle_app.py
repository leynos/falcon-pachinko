"""Compose the Falcon application used by lifecycle behaviour tests."""

from __future__ import annotations

import dataclasses as dc
import typing as typ

import falcon.asgi

from falcon_pachinko import WebSocketConnectionManager, WebSocketRouter

if typ.TYPE_CHECKING:
    from falcon_pachinko import HookContext

from tests.behaviour._live_lifecycle_resources import (
    AcceptanceCountingWebSocket,
    DisconnectRecorder,
    LifecycleParentResource,
    RecordingOptions,
    RecordingResource,
    RejectingResource,
)


@dc.dataclass(slots=True)
class LiveApplication:
    """Application pieces shared by the live server and BDD assertions."""

    app: falcon.asgi.App[falcon.asgi.Request, falcon.asgi.Response]
    router: WebSocketRouter
    manager: WebSocketConnectionManager
    recorder: DisconnectRecorder


def create_live_application() -> LiveApplication:
    """Build Falcon with a router mounted on the exact and catch-all paths."""
    app: falcon.asgi.App[falcon.asgi.Request, falcon.asgi.Response] = falcon.asgi.App()
    manager = WebSocketConnectionManager()
    recorder = DisconnectRecorder()
    router = WebSocketRouter(
        simulator_factory=lambda _req, ws: AcceptanceCountingWebSocket(ws, recorder)
    )
    _add_live_routes(router, manager, recorder)
    _add_global_hooks(router, recorder)
    prefix = "/ws"
    router.mount(prefix)
    app.add_route(prefix, router)
    app.add_route(prefix + "/{pachinko_path:path}", router)
    return LiveApplication(app, router, manager, recorder)


def _add_live_routes(
    router: WebSocketRouter,
    manager: WebSocketConnectionManager,
    recorder: DisconnectRecorder,
) -> None:
    """Register the focused endpoints exercised by live lifecycle scenarios."""
    router.add_route(
        "/session",
        RecordingResource,
        manager=manager,
        recorder=recorder,
    )
    router.add_route("/reject", RejectingResource, recorder=recorder)
    router.add_route(
        "/reject-close", RejectingResource, recorder=recorder, close_on_connect=True
    )
    router.add_route(
        "/accept-close",
        RecordingResource,
        manager=manager,
        recorder=recorder,
        options=RecordingOptions(close_on_connect=True),
    )
    router.add_route(
        "/start-failure",
        RecordingResource,
        manager=manager,
        recorder=recorder,
        options=RecordingOptions(fail_session_start=True),
    )
    router.add_route(
        "/hello",
        RecordingResource,
        manager=manager,
        recorder=recorder,
        options=RecordingOptions(hello_exchange=True),
    )
    router.add_route(
        "/decoded-media",
        RecordingResource,
        manager=manager,
        recorder=recorder,
        options=RecordingOptions(receive_decoded_media=True),
    )
    router.add_route(
        "/deprecated-accept",
        RecordingResource,
        manager=manager,
        recorder=recorder,
        options=RecordingOptions(legacy_accept=True),
    )
    router.add_route(
        "/lifecycle",
        LifecycleParentResource,
        manager=manager,
        recorder=recorder,
    )


def _add_global_hooks(router: WebSocketRouter, recorder: DisconnectRecorder) -> None:
    """Record each global hook in the live lifecycle event stream."""
    for event in (
        "before_connect",
        "after_connect",
        "before_receive",
        "after_receive",
        "before_disconnect",
    ):
        if event == "before_disconnect":

            def record_disconnect(context: HookContext) -> None:
                """Capture the cleanup error while recording the global hook order."""
                recorder.events.append("global.before_disconnect")
                recorder.before_disconnect_errors.append(context.error)

            router.global_hooks.add(event, record_disconnect)
        else:
            router.global_hooks.add(
                event,
                lambda _context, name=event: recorder.events.append(f"global.{name}"),
            )
