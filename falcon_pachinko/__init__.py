"""falcon-pachinko package."""

from __future__ import annotations

import falcon.asgi

from .connection_manager import WebSocketConnectionManager

PACKAGE_NAME = "falcon_pachinko"

try:  # pragma: no cover - Rust optional
    rust = __import__(f"_{PACKAGE_NAME}_rs")
    hello = rust.hello  # type: ignore[attr-defined]
except ModuleNotFoundError:  # pragma: no cover - Python fallback
    from .pure import hello

_MANAGERS: dict[int, WebSocketConnectionManager] = {}


def _get_ws_manager(app: falcon.asgi.App) -> WebSocketConnectionManager:
    app_id = id(app)
    if app_id not in _MANAGERS:
        raise RuntimeError(
            "WebSocketConnectionManager has not been installed for this Falcon app."
        )
    return _MANAGERS[app_id]

__all__ = ["hello", "WebSocketConnectionManager", "install"]


def install(app: falcon.asgi.App) -> None:
    """Attach a :class:`WebSocketConnectionManager` to ``app``."""
    app_id = id(app)
    if app_id in _MANAGERS:
        raise RuntimeError("WebSocketConnectionManager already installed")
    _MANAGERS[app_id] = WebSocketConnectionManager()
    if not hasattr(falcon.asgi.App, "ws_connection_manager"):
        falcon.asgi.App.ws_connection_manager = property(_get_ws_manager)  # type: ignore[attr-defined]
