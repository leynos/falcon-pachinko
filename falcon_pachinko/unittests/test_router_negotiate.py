"""Public route negotiation behavior tests."""

from __future__ import annotations

import falcon
import pytest

from falcon_pachinko import WebSocketRouter
from falcon_pachinko.unittests._router_test_resources import AcceptingResource
from falcon_pachinko.unittests.helpers import DummyWS, make_req


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("path", "matches"),
    [("/ok", True), ("/oops", False)],
    ids=["matching_path", "unmatched_path"],
)
async def test_negotiate_matches_registered_path(path: str, *, matches: bool) -> None:
    """Public negotiation accepts a matching route and rejects an unmatched path."""
    router = WebSocketRouter()
    router.add_route("/ok", AcceptingResource)
    router.mount("/")
    req = make_req(path, "/")
    ws = DummyWS()

    if not matches:
        with pytest.raises(falcon.HTTPNotFound):
            await router.negotiate(req, ws)
        return

    session = await router.negotiate(req, ws)
    assert session is not None, "matching route should return an accepted session"
    assert ws.accepted, "matching route should accept its WebSocket"
    await session.aclose()
