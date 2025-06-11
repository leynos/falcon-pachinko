from __future__ import annotations

from typing import Any, cast

from falcon_pachinko import install


class DummyApp:
    pass


def test_install_adds_methods_and_manager() -> None:
    app = DummyApp()
    install(app)  # type: ignore[arg-type]
    app_any = cast(Any, app)

    assert hasattr(app_any, "ws_connection_manager")
    assert isinstance(app_any.ws_connection_manager, object)
    assert callable(app_any.add_websocket_route)


def test_add_websocket_route_registers_resource() -> None:
    app = DummyApp()
    install(app)  # type: ignore[arg-type]
    app_any = cast(Any, app)

    resource = object()
    app_any.add_websocket_route("/ws", resource)

    assert app_any._websocket_routes["/ws"] is resource
