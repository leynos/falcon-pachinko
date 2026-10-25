"""Tests for Falcon attachment and retry behavior."""

from __future__ import annotations

import typing as typ

import falcon.asgi
import pytest

from falcon_pachinko import WebSocketResource, WebSocketRouter


@pytest.mark.parametrize(
    "failing_route_index",
    [0, 1],
    ids=("mount_route", "descendant_route"),
)
def test_attach_can_retry_after_route_registration_failure(
    failing_route_index: int,
) -> None:
    """A failed Falcon route registration must leave the router reusable."""
    router = WebSocketRouter()
    router.add_route("/rooms/{room}", WebSocketResource)

    class FailingFalconApp(falcon.asgi.App):
        def __init__(self) -> None:
            super().__init__()
            self._failed_once = False
            self._route_registration_index = 0

        @typ.override
        def add_route(
            self,
            uri_template: str,
            resource: object,
            **kwargs: object,
        ) -> None:
            route_index = self._route_registration_index
            self._route_registration_index += 1
            if route_index == failing_route_index and not self._failed_once:
                self._failed_once = True
                # Rollback must recover keys removed during registration too.
                del self.ws_options.media_handlers[falcon.WebSocketPayloadType.BINARY]
                msg = "route registration failed"
                raise RuntimeError(msg)
            super().add_route(uri_template, resource, **kwargs)

    app = FailingFalconApp()
    original_handlers = dict(app.ws_options.media_handlers)
    with pytest.raises(RuntimeError, match="route registration failed"):
        router.attach(app, "/ws")

    assert dict(app.ws_options.media_handlers) == original_handlers, (
        "attach failure must restore the app media handlers"
    )
    assert not router._mount_prefix, "attach failure must clear the mount prefix"
    assert not router._routes, "attach failure must clear registered routes"

    router.attach(app, "/ws")

    assert router._mount_prefix == "/ws", (
        "retry after route registration failure should mount the router"
    )
