# Falcon-Pachinko User Guide

This guide explains how to build WebSocket applications with the composable
architecture introduced in Falcon-Pachinko. It focuses on the recommended
router/resource model, then dives into advanced patterns such as dependency
injection, shared state, custom connection manager backends, background
workers, and testing utilities.

## 1. Core Concepts

- **WebSocketRouter** – Mountable Falcon resource that owns route definitions
  and orchestrates instantiation.
- **WebSocketResource** – Per-connection handler exposing lifecycle hooks
  (`on_connect`, `on_disconnect`) and message handlers.
- **Composable hierarchy** – Resources can register child routes with
  `add_subroute`, passing context and shared state down the chain.
- **Schema-driven dispatch** – Define a `msgspec` tagged union `schema` and
  register handlers with `@handles_message("tag")` (preferred) or `on_tag`
  method names.
- **Connection manager** – `WebSocketConnectionManager` tracks active sockets,
  rooms, and supports pluggable backends.
- **Workers** – ASGI lifespan-friendly background tasks coordinated by
  `WorkerController`.

## 2. Quickstart (Composable Routing)

```python
import falcon
from falcon_pachinko import WebSocketResource, WebSocketRouter, handles_message


class ChatResource(WebSocketResource):
    async def on_connect(self, req, ws, room: str) -> bool:
        await ws.accept()
        self.state["room"] = room
        return True  # continue to message handling

    @handles_message("chat.message")
    async def handle_message(self, ws, payload):
        await ws.send_media({"type": "echo", "text": payload.text})


app = falcon.App()
router = WebSocketRouter()
router.add_route("/chat/{room}", ChatResource)
app.add_route("/ws", router)  # router is a Falcon resource
router.mount("/ws")
```

- The router is mounted once (`router.mount("/ws")`) and handles all descendant
  paths relative to that prefix.
- Each connection receives a **fresh resource instance** and a **shared state
  proxy** scoped to that connection.

## 3. Resource Lifecycle & State

- `on_connect(req, ws, **params) -> bool | None`
  - Accept/close/inspect headers, seed `self.state`, return `False` to stop
    processing after connect.
- `on_disconnect(req, ws, close_code, **params) -> None`
  - Clean up resources; runs even if connection negotiation fails after
    acceptance.
- `self.state`
  - Dict-like proxy shared across all resources in the same connection chain.
  - Override via `get_child_context()` to supply a custom state store (e.g.,
    Redis-backed proxy).

### Nested resources

```python
class Parent(WebSocketResource):
    def __init__(self):
        self.state["parent_ready"] = True
        self.add_subroute("child/{item}", Child)

    def get_child_context(self):
        return {"project": "acme"}  # merged into child kwargs


class Child(WebSocketResource):
    def __init__(self, project: str):
        self.project = project

    async def on_connect(self, req, ws, item: str) -> bool:
        self.state["child_item"] = item
        return False
```

- Path params flow into each resource; parent-provided context merges with
  params for the next child.
- State defaults to a shared proxy unless overridden in `get_child_context()`.

## 4. Schema-Driven Message Dispatch

```python
import msgspec
from falcon_pachinko import handles_message


class Message(msgspec.Struct, tag=True):
    type: str


class Join(Message, tag="join"):
    room: str


class ChatResource(WebSocketResource):
    schema = msgspec.defstruct(Join)  # tagged union

    @handles_message("join")
    async def on_join(self, ws, payload: Join):
        await ws.send_media({"type": "joined", "room": payload.room})
```

- Messages are decoded with `msgspec`; unknown tags fall back to
  `on_unhandled(self, ws, raw)` when defined.
- The decorator supports `strict=False` to allow extra fields when required.

## 5. Hooks

- Register global hooks on the router or per-resource hooks (`before_connect`,
  `after_connect`, `before_message`, `after_message`, etc.).
- Execution order is onion-style: global → parent → child → handler → child →
  parent → global.
- Raise from a `before_*` hook to terminate negotiation early.

## 6. Dependency Injection (How-to)

1) **Choose a factory** – Pass `resource_factory` to `WebSocketRouter`.
2) **Provide services** – Register shared services in the container.
3) **Mount the router** – All route instantiation flows through your factory.

```python
from falcon_pachinko import ServiceContainer, WebSocketRouter

conn_mgr = app.ws_connection_manager
container = ServiceContainer()
container.register("conn_mgr", conn_mgr)
container.register("db", my_db_pool)

router = WebSocketRouter(resource_factory=container.create_resource)
router.add_route("/rooms/{room}", ChatResource)
router.mount("/ws")
```

### Test-friendly factories

```python
from falcon_pachinko.unittests.resource_factories import resource_factory

router = WebSocketRouter(resource_factory=resource_factory(fake_service))
```

- Inject mocks/spies in tests without bootstrapping the production container.
- See `tests/behaviour/dependency_injection.feature` for an end-to-end example.

## 7. State Management (How-to)

Goal: keep per-connection data mutable yet replaceable.

- **Default:** `self.state` is an in-memory dict shared across nested resources.
- **External store:** Provide `state` via `get_child_context()` to replace the
  proxy with your own mapping or adapter.
- **Pattern:** Use lightweight objects that implement the `MutableMapping`
  protocol so resources remain agnostic to the backing store.

```python
class Parent(WebSocketResource):
    def get_child_context(self):
        return {"state": redis_state_proxy(connection_id=self.scope["id"])}
```

- Hooks are a good place to pre-fill state (e.g., authenticated user,
  transaction IDs).
- Tests can assert state transitions via `WebSocketSimulator` or
  `WebSocketTestClient`.

## 8. Custom Connection Manager Backends (How-to)

`WebSocketConnectionManager` delegates storage to a `ConnectionBackend`
implementation. Swap in your own to support clustering or observability.

```python
from falcon_pachinko.websocket import ConnectionBackend, WebSocketConnectionManager


class RedisBackend(ConnectionBackend):
    ...


conn_mgr = WebSocketConnectionManager(backend=RedisBackend(...))
app.ws_connection_manager = conn_mgr  # before router mounts
```

Backend requirements (see `ConnectionBackend` ABC):

- Raise `ValueError` on duplicate `add_connection`.
- Make `leave_room` and `remove_connection` idempotent.
- Provide read-only `websockets`/`rooms` views and a consistent `snapshot`.
- Ignore or document stale room memberships in `snapshot`.

Tests illustrating this pattern live in:

- `tests/test_connection_manager_unit.py::test_manager_uses_custom_backend`
- `tests/behaviour/custom_connection_backend.feature`

## 9. Background Workers

- Use `WorkerController` to start/stop async tasks during ASGI lifespan.
- Prefer `@app.lifespan` to wire startup/shutdown; workers can depend on the
  same DI container used for WebSocket resources.

```python
from falcon_pachinko import WorkerController, worker

controller = WorkerController()

@worker
async def broadcaster(*, conn_mgr):
    async for ws in conn_mgr.connections():
        await ws.send_media({"type": "ping"})

@app.lifespan
async def lifespan(app):
    await controller.start(broadcaster, conn_mgr=app.ws_connection_manager)
    yield
    await controller.stop()
```

## 10. Testing Toolkit

- **WebSocketTestClient** – Real websocket client powered by `websockets`,
  designed for integration tests; captures traces for assertions.
- **WebSocketSimulator** – In-memory fake implementing the WebSocket protocol
  for fast unit tests.
- **Pytest fixtures** – See `tests/behaviour/*.feature` and
  `falcon_pachinko/unittests` helpers for factory utilities.

Recommended strategy:

- Unit test pure resource logic with `WebSocketSimulator`.
- Behavioural tests with `pytest-bdd` to exercise router composition, hooks,
  DI, and connection manager flows.
- Worker tests using the `WorkerController` fixture from
  `tests/behaviour/lifespan_workers.feature`.

## 11. Reference Example

`examples/reference_app` wires together:

- Router mounted at `/ws`
- Schema-driven resources composed as parent/child/grandchild
- Router-level DI via `ServiceContainer`
- Connection manager usage from both resources and workers
- Behavioural coverage via `pytest-bdd`

Use it as a blueprint for production deployments and adapt the patterns shown
there to your own DI container, state store, and connection backend.
