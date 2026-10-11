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
import falcon.asgi
from falcon_pachinko import WebSocketResource, WebSocketRouter, handles_message


class ChatResource(WebSocketResource):
    async def on_connect(self, req, ws, room: str) -> bool:
        self.state["room"] = room
        return True  # continue to message handling

    @handles_message("chat.message")
    async def handle_message(self, ws, payload):
        await ws.send_media({"type": "echo", "text": payload.text})


app = falcon.asgi.App()
router = WebSocketRouter()
router.add_route("/chat/{room}", ChatResource)
router.mount("/ws")
app.add_route("/ws", router)
app.add_route("/ws/{pachinko_path:path}", router)
```

- The router is mounted once (`router.mount("/ws")`). In Falcon, register it
  both at the exact prefix and at one descendant route ending in
  `/{pachinko_path:path}` so it receives descendant paths. The router matches
  using the concrete request path.
- `on_connect` runs before router acceptance. Return `True` to ask the router
  to accept; do not also call `ws.accept()` in this pattern. A resource may
  instead call `ws.accept()` itself and return `True`; the router detects that
  acceptance and will not accept twice.
- Each connection receives a **fresh resource instance** and a **shared state
  proxy** scoped to that connection.

### The `add_route` signature

```python
def add_route(
    self,
    path: str,
    resource: type[WebSocketResource] | cabc.Callable[..., WebSocketResource],
    *init_args: object,
    name: str | None = None,
    **init_kwargs: object,
) -> None: ...
```

- `*init_args` and `**init_kwargs` are stored and passed to `resource` when a
  connection instantiates it, alongside the path parameters. Example passing
  both a positional and a keyword initializer argument:

  ```python
  router.add_route("/rooms/{room}", ChatResource, "lobby", history_size=100)
  ```

- `name` is reserved for naming the route itself and is never forwarded to
  the resource initializer.
- `resource` accepts any callable returning a `WebSocketResource`, not only a
  class, so a `functools.partial` factory covers a resource whose own
  initializer needs a parameter called `name`:

  ```python
  router.add_route("/p", functools.partial(MyResource, name="x"), name="route")
  ```

## 3. Resource Lifecycle & State

- `on_connect(req, ws, **params) -> bool | None`
  - Runs before router acceptance. Inspect headers, seed `self.state`, and
    return a truthy value to approve the session. Returning a falsey value
    rejects it. A resource may accept the socket itself when acceptance must
    precede resource setup; the router then owns its subsequent session loop.
- `on_disconnect(ws, close_code) -> None`
  - Called exactly once after approval or resource-owned acceptance. Use it to
    release per-session resources, including connection-manager membership.
    Rejected, never-accepted connections do not run post-accept cleanup.
- `self.state`
  - Dict-like proxy shared across all resources in the same connection chain.
  - Override via `get_child_context()` to supply a custom state store (e.g.,
    Redis-backed proxy).

### Disconnect and cleanup contract

Once `on_connect` approves the connection or a resource accepts it, the router
owns the session through disconnect cleanup. `on_disconnect(ws, close_code)`
runs exactly once, including when setup fails after this establishment point.
This lets a resource roll back registrations made during `on_connect`. A
rejected connection that was never accepted skips this post-accept cleanup.
This includes rejection by calling `ws.close()` during `on_connect`. Accepting
and then closing during `on_connect` still promises cleanup. An approved socket
that never becomes accepted is cleaned up with `1011` without receiving frames.

| Session ending                                    | `close_code` passed to `on_disconnect`                                                               |
| ------------------------------------------------- | ---------------------------------------------------------------------------------------------------- |
| Client sends a close frame                        | The close code Falcon reports                                                                        |
| Resource or server initiates a close              | Falcon's reported disconnect code, when available; the requested outbound code is sent to the client |
| Transport disappears without a close frame        | `1006` (abnormal closure)                                                                            |
| Task is cancelled                                 | `1001` (going away); the router attempts to close with this code                                     |
| Handler or setup fails with an ordinary exception | `1011` (internal error); the router attempts to close with this code                                 |

Falcon's `WebSocketDisconnected` is a transport ending even when a resource
observes it while sending or during accepted setup; its reported code is
retained. Cancellation after disconnect cleanup begins does not rewrite the
close code already selected.

The router runs `before_disconnect` before `on_disconnect`, then marks the
session closed. Once disconnect begins, receive and dispatch hooks do not run
for later frames. Cleanup runs in a shielded task and has no timeout, so
`on_disconnect` must terminate. A cleanup exception does not replace a lone
session exception; when both are ordinary exceptions, they are raised in an
`ExceptionGroup` with the session exception first. A single failure is raised
unchanged. Cancellation remains a bare `CancelledError`; cleanup failures that
cannot be grouped with it are reported to the event loop's exception handler.

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

The stable import path is `from falcon_pachinko.utils import ValidationError`.
It is the same class as `msgspec.ValidationError`, so existing
`except msgspec.ValidationError` handlers continue to work. The dispatcher
routes validation failures to `on_unhandled`; a `ValidationError` raised inside
a handler propagates.

Native text and binary frames reach the dispatcher unchanged. Malformed JSON
can therefore reach `on_unhandled(self, ws, raw)` without Falcon's media
decoder ending the connection first. Falcon's public media sending methods
retain their usual arguments, including `send_media(media=...)`.

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


class RedisBackend(ConnectionBackend): ...


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
- If `start()` fails while creating or scheduling a worker, it closes any
  rejected coroutine, cancels and awaits tasks already scheduled, resets the
  controller and re-raises the original error. The same controller can then be
  started again. Rollback failures are attached to the original error as notes.

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

The `@worker` decorator is optional and marks intent only.
`WorkerController.start()` runs only the workers that the application passes to
it. An undecorated async function works in the same way.

## 10. Testing Toolkit

- **WebSocketTestClient** – Real websocket client powered by `websockets`,
  designed for integration tests; captures traces for assertions.
- **WebSocketSimulator** – In-memory fake implementing the WebSocket protocol
  for fast unit tests.
- **Pytest fixtures** – See `tests/behaviour/*.feature` and
  `falcon_pachinko/unittests` helpers for factory utilities.

The `websocket_simulator` pytest fixture (from `falcon_pachinko.testing`) takes
no parameters and yields a `SimulatorRouterHarness`: a pre-mounted router and
app wired to inject a `WebSocketSimulator` into each connection. Register
routes on `harness.router`, then dispatch a connection with
`async with harness.connect(path) as connection: ...`. On teardown the fixture
calls `harness.discard_pending_simulator()`, discarding any simulator staged
for a connection that was never established, so tests do not need to assemble a
router and simulator by hand.

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
