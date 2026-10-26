"""Executable checks for the users-guide quick-start example."""

from __future__ import annotations

import asyncio
import importlib.util
import re
import sys
import typing as typ
from pathlib import Path

import falcon.asgi
import pytest

from falcon_pachinko.testing import LiveWebSocketServer, WebSocketTestClient

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    from types import ModuleType


def _extract_quickstart_source(guide: str) -> str:
    """Return the Python fence from the composable-routing quick-start."""
    heading = "## 2. Quickstart (Composable Routing)"
    heading_position = guide.find(heading)
    if heading_position == -1:
        pytest.fail(f"users guide is missing the {heading!r} heading")

    section = guide[heading_position + len(heading) :]
    next_heading = re.search(r"^#{1,6}\s+.*$", section, re.MULTILINE)
    if next_heading is not None:
        section = section[: next_heading.start()]

    fence = re.search(
        r"^```python[ \t]*\r?\n(?P<source>.*?)^```[ \t]*$",
        section,
        re.MULTILINE | re.DOTALL,
    )
    if fence is None:
        pytest.fail(f"{heading!r} is missing its Python code fence")
    return fence.group("source")


@pytest.fixture
def quickstart_module(tmp_path: Path) -> cabc.Iterator[ModuleType]:
    """Load the first Python example in the composable-routing quick-start."""
    guide_path = Path(__file__).resolve().parents[1] / "docs" / "users-guide.md"
    guide = guide_path.read_text(encoding="utf-8")
    quickstart_path = tmp_path / "quickstart.py"
    quickstart_path.write_text(_extract_quickstart_source(guide), encoding="utf-8")
    module_name = "users_guide_quickstart"
    spec = importlib.util.spec_from_file_location(module_name, quickstart_path)
    if spec is None or spec.loader is None:
        pytest.fail(f"could not create an import spec for {quickstart_path}")

    module = importlib.util.module_from_spec(spec)
    previous_module = sys.modules.get(module_name)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
        yield module
    finally:
        if previous_module is None:
            sys.modules.pop(module_name, None)
        else:
            sys.modules[module_name] = previous_module


def test_quickstart_constructs_falcon_asgi_app(
    quickstart_module: ModuleType,
) -> None:
    """The documented application is the Falcon ASGI application."""
    assert isinstance(quickstart_module.app, falcon.asgi.App), (
        "the quick-start must expose a Falcon ASGI app"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/ws/chat/lobby", "/ws"])
async def test_quickstart_echoes_a_typed_websocket_message(
    quickstart_module: ModuleType,
    path: str,
) -> None:
    """A live connection reaches the mounted room route and echoes its text."""
    async with LiveWebSocketServer(quickstart_module.app) as server:
        client = WebSocketTestClient(server.base_url, allow_insecure=True)
        async with client.connect(path) as session:
            await session.send_json({"type": "chat.message"})
            await session.send_json({"type": "chat.message", "payload": {"text": "hi"}})
            async with asyncio.timeout(5):
                response = await session.receive_json()

    assert response == {"type": "echo", "text": "hi"}, (
        "the quick-start must echo the decoded ChatPayload text"
    )


@pytest.mark.asyncio
async def test_quickstart_adapter_preserves_request_headers(
    quickstart_module: ModuleType,
) -> None:
    """A lifecycle hook can read headers from Falcon's routed request."""
    observed_headers: list[str | None] = []

    class HeaderProbeResource(quickstart_module.ChatResource):
        async def on_connect(
            self,
            req: falcon.asgi.Request,
            ws: falcon.asgi.WebSocket,
            room: str = "lobby",
        ) -> bool:
            observed_headers.append(req.get_header("X-Quickstart-Test"))
            return await super().on_connect(req, ws, room)

    quickstart_module.router.add_route("/header-probe", HeaderProbeResource)
    async with LiveWebSocketServer(quickstart_module.app) as server:
        client = WebSocketTestClient(server.base_url, allow_insecure=True)
        async with client.connect(
            "/ws/header-probe", headers={"X-Quickstart-Test": "preserved"}
        ) as session:
            await session.send_json({"type": "chat.message", "payload": {"text": "hi"}})
            async with asyncio.timeout(5):
                response = await session.receive_json()

    assert observed_headers == ["preserved"], (
        "the adapter must preserve Falcon request header access"
    )
    assert response == {"type": "echo", "text": "hi"}, (
        "the quick-start must echo the decoded ChatPayload text"
    )
