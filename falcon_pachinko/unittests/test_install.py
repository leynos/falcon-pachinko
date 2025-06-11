from __future__ import annotations

import falcon.asgi

import falcon_pachinko
import pytest


def test_install_attaches_connection_manager() -> None:
    app = falcon.asgi.App()
    falcon_pachinko.install(app)
    manager = getattr(app, "ws_connection_manager")
    assert isinstance(manager, falcon_pachinko.WebSocketConnectionManager)


def test_install_distinct_apps_get_distinct_managers() -> None:
    app1 = falcon.asgi.App()
    app2 = falcon.asgi.App()
    falcon_pachinko.install(app1)
    falcon_pachinko.install(app2)
    manager1 = getattr(app1, "ws_connection_manager")
    manager2 = getattr(app2, "ws_connection_manager")
    assert isinstance(manager1, falcon_pachinko.WebSocketConnectionManager)
    assert isinstance(manager2, falcon_pachinko.WebSocketConnectionManager)
    assert manager1 is not manager2


def test_install_fails_when_already_installed() -> None:
    app = falcon.asgi.App()
    falcon_pachinko.install(app)
    with pytest.raises(RuntimeError):
        falcon_pachinko.install(app)
