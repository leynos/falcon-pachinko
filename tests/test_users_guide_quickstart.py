"""Executable checks for the users-guide quick-start example."""

from __future__ import annotations

import asyncio
import dataclasses as dc
import importlib.util
import re
import sys
import typing as typ
from pathlib import Path

import falcon.asgi
import pytest
from hypothesis import example, given, settings
from hypothesis import strategies as st

from falcon_pachinko.testing import LiveWebSocketServer, WebSocketTestClient

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    from types import ModuleType

    from falcon_pachinko import HookContext


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


@dc.dataclass(frozen=True, slots=True)
class _Fence:
    """Describe source independently of its rendered Markdown fence."""

    language: str
    source_lines: tuple[str, ...]
    prose: str = ""

    def render(self, newline: str) -> str:
        """Render one complete triple-backtick fence with leading prose."""
        return newline.join([
            self.prose,
            f"```{self.language}",
            *self.source_lines,
            "```",
        ])


@dc.dataclass(frozen=True, slots=True)
class _GuideLayout:
    """Keep target-section membership separate from the rendered document."""

    before: tuple[_Fence, ...] = ()
    target: tuple[_Fence, ...] = ()
    after: tuple[_Fence, ...] = ()
    newline: str = "\n"
    following_level: int = 2
    prose: str = "Some explanatory prose."

    def render(self) -> str:
        """Render the three modeled sections without parsing their contents."""
        parts = [
            *(fence.render(self.newline) for fence in self.before),
            "## 2. Quickstart (Composable Routing)",
            self.prose,
            *(fence.render(self.newline) for fence in self.target),
            f"{'#' * self.following_level} Following section",
            self.prose,
            *(fence.render(self.newline) for fence in self.after),
        ]
        return self.newline.join(parts) + self.newline


_FENCES = st.builds(
    _Fence,
    language=st.sampled_from(["python", "sh", "text", ""]),
    source_lines=st.lists(
        st.sampled_from([
            "app = object()",
            "# Configure app.",
            "## A code comment",
            "",
        ]),
        min_size=1,
        max_size=3,
    ).map(tuple),
    prose=st.sampled_from(["", "Explanation before this fence."]),
)
_FENCE_SEQUENCES = st.lists(_FENCES, max_size=3).map(tuple)
_GUIDE_LAYOUTS = st.builds(
    _GuideLayout,
    before=_FENCE_SEQUENCES,
    target=_FENCE_SEQUENCES,
    after=_FENCE_SEQUENCES,
    newline=st.sampled_from(["\n", "\r\n"]),
    following_level=st.integers(min_value=1, max_value=6),
    prose=st.sampled_from(["", "Some explanatory prose."]),
)


@settings(max_examples=80)
@given(layout=_GUIDE_LAYOUTS)
@example(
    layout=_GuideLayout(
        target=(_Fence("python", ("# Configure app.", "app = object()")),),
        newline="\r\n",
    )
)
@example(
    layout=_GuideLayout(
        target=(_Fence("sh", ("# A shell comment",)),),
        after=(_Fence("python", ("later = object()",)),),
    )
)
def test_extract_quickstart_source_matches_generated_layout(
    layout: _GuideLayout,
) -> None:
    """Extraction agrees with section descriptors across bounded layouts."""
    python_fences = [fence for fence in layout.target if fence.language == "python"]
    guide = layout.render()

    if not python_fences:
        with pytest.raises(
            pytest.fail.Exception, match="missing its Python code fence"
        ):
            _extract_quickstart_source(guide)
    else:
        expected = layout.newline.join(python_fences[0].source_lines) + layout.newline
        assert _extract_quickstart_source(guide) == expected, (
            "the first Python descriptor in the target section defines the source"
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
@pytest.mark.parametrize(
    ("path", "expected_room"),
    [("/ws/chat/lobby", "lobby"), ("/ws/chat/club", "club"), ("/ws", "lobby")],
    ids=["explicit-default-room", "non-default-room", "exact-mount-default"],
)
async def test_quickstart_echoes_a_typed_websocket_message(
    quickstart_module: ModuleType,
    path: str,
    expected_room: str,
) -> None:
    """The documented resource receives its room and echoes messages in order."""
    observed_rooms: list[object] = []

    def record_room(context: HookContext) -> None:
        """Observe the documented resource after its connection hook finishes."""
        assert type(context.target) is quickstart_module.ChatResource, (
            "the route must instantiate the documented ChatResource"
        )
        observed_rooms.append(context.target.state["room"])

    quickstart_module.router.global_hooks.add("after_connect", record_room)
    texts = ["hi", "a second message"]
    responses = []
    async with LiveWebSocketServer(quickstart_module.app) as server:
        client = WebSocketTestClient(server.base_url, allow_insecure=True)
        async with client.connect(path) as session:
            for text in texts:
                await session.send_json({
                    "type": "chat.message",
                    "payload": {"text": text},
                })
            for _ in texts:
                async with asyncio.timeout(5):
                    responses.append(await session.receive_json())

    assert observed_rooms == [expected_room], (
        "the connection hook must receive the route's room or the exact-mount default"
    )
    assert responses == [{"type": "echo", "text": text} for text in texts], (
        "the quick-start must echo each decoded ChatPayload text in message order"
    )


@pytest.mark.asyncio
async def test_quickstart_ignores_missing_payload_and_keeps_connection_open(
    quickstart_module: ModuleType,
) -> None:
    """An absent payload emits no frame and leaves the session usable."""
    async with LiveWebSocketServer(quickstart_module.app) as server:
        client = WebSocketTestClient(server.base_url, allow_insecure=True)
        async with client.connect("/ws/chat/lobby") as session:
            await session.send_json({"type": "chat.message"})
            with pytest.raises(TimeoutError, match=r"^$"):
                async with asyncio.timeout(0.2):
                    await session.receive_json()

            await session.send_json({
                "type": "chat.message",
                "payload": {"text": "after missing payload"},
            })
            async with asyncio.timeout(5):
                response = await session.receive_json()

    assert response == {"type": "echo", "text": "after missing payload"}, (
        "the connection must still echo a valid message after an absent payload"
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
