"""Tests for Falcon attachment and retry behavior."""

from __future__ import annotations

import typing as typ

import falcon.asgi
import pytest

from falcon_pachinko import WebSocketResource, WebSocketRouter


@pytest.mark.parametrize(
    "failing_route",
    ["/ws", "/ws/{pachinko_path:path}"],
    ids=("mount_route", "descendant_route"),
)
def test_attach_can_retry_after_route_registration_failure(
    failing_route: str,
) -> None:
    """A failed Falcon route registration must leave the router reusable."""
    router = WebSocketRouter()
    router.add_route("/rooms/{room}", WebSocketResource)

    class FailingFalconApp(falcon.asgi.App):
        def __init__(self) -> None:
            super().__init__()
            self._failed_once = False

        @typ.override
        def add_route(
            self,
            uri_template: str,
            resource: object,
            **kwargs: object,
        ) -> None:
            if uri_template == failing_route and not self._failed_once:
                self._failed_once = True
                msg = "route registration failed"
                raise RuntimeError(msg)
            super().add_route(uri_template, resource, **kwargs)

    app = FailingFalconApp()
    with pytest.raises(RuntimeError, match="route registration failed"):
        router.attach(app, "/ws")

    router.attach(app, "/ws")

    assert router._mount_prefix == "/ws", (
        "retry after route registration failure should mount the router"
    )
