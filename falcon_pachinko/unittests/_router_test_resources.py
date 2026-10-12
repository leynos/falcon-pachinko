"""Small shared resources for router unit tests."""

from __future__ import annotations

import typing as typ

from falcon_pachinko import WebSocketResource


class DummyResource(WebSocketResource):
    """Capture connection parameters for testing."""

    instances: typ.ClassVar[list[DummyResource]] = []

    def __init__(self) -> None:
        DummyResource.instances.append(self)

    async def on_connect(self, req: object, ws: object, **params: object) -> bool:
        """Record params and refuse the connection."""
        self.params = params
        return False


class AcceptingResource(WebSocketResource):
    """Resource that always accepts the connection."""

    @typ.override
    async def on_connect(self, req: object, ws: object, **params: object) -> bool:
        """Signal that the connection should be accepted."""
        return True
