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
    # Consume complete fences before considering headings inside their source.
    blocks = re.finditer(
        r"^```(?P<language>[^\r\n]*)\r?\n(?P<source>.*?)^```[ \t]*\r?$"
        r"|^#{1,6}[ \t]+[^\r\n]*\r?$",
        section,
        re.MULTILINE | re.DOTALL,
    )
    python_source: str | None = None
    for block in blocks:
        language = block.group("language")
        if language is None:
            break
        if language.strip() == "python":
            python_source = block.group("source")
            break
    if python_source is None:
        pytest.fail(f"{heading!r} is missing its Python code fence")
    return python_source


def test_extract_quickstart_source_preserves_python_comments() -> None:
    """A Python comment inside the fence is source, not a section boundary."""
    guide = """## 2. Quickstart (Composable Routing)

```python
# Configure the application.
app = object()
```
"""

    assert _extract_quickstart_source(guide) == (
        "# Configure the application.\napp = object()\n"
    ), "extracting the quick-start must retain unindented Python comments"


def test_extract_quickstart_source_requires_the_target_heading() -> None:
    """An unrelated Python fence cannot replace the quick-start heading."""
    guide = """## Another section

```python
app = object()
```
"""

    with pytest.raises(pytest.fail.Exception, match="users guide is missing the"):
        _extract_quickstart_source(guide)


def test_extract_quickstart_source_rejects_a_later_sections_fence() -> None:
    """A missing quick-start fence must fail before the next section's code."""
    guide = """## 2. Quickstart (Composable Routing)

There is no example here.

## Another section

```python
later = object()
```
"""

    with pytest.raises(pytest.fail.Exception, match="missing its Python code fence"):
        _extract_quickstart_source(guide)


def test_extract_quickstart_source_uses_the_first_python_fence() -> None:
    """Only the first Python example in the target section is returned."""
    guide = """```python
before = object()
```

## 2. Quickstart (Composable Routing)

```sh
# Prepare the environment.
uv sync
```

```python
first = object()
```

```python
second = object()
```

## Another section

```python
later = object()
```
"""

    assert _extract_quickstart_source(guide) == "first = object()\n", (
        "the extractor must return the first Python fence after the target heading"
    )


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
