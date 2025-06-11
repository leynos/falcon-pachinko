from __future__ import annotations

import falcon.asgi

import falcon_ws
import pytest


def test_install_attaches_connection_manager() -> None:
    app = falcon.asgi.App()
    falcon_ws.install(app)
    manager = getattr(app, "ws_connection_manager")
    assert isinstance(manager, falcon_ws.WebSocketConnectionManager)


def test_install_fails_when_already_installed() -> None:
    app = falcon.asgi.App()
    falcon_ws.install(app)
    with pytest.raises(RuntimeError):
        falcon_ws.install(app)
