# Falcon-Pachinko User Guide

This guide explains how to build WebSocket applications with the composable
architecture introduced in Falcon-Pachinko. It focuses on the recommended
router/resource model, then dives into advanced patterns such as dependency
injection, shared state, custom connection manager backends, background
workers, and testing utilities.

## 1. Core Concepts

- **WebSocketRouter** – Falcon-attached router that owns route definitions and
  orchestrates resource instantiation.
- **WebSocketResource** – Per-connection handler exposing lifecycle hooks
  (`on_connect`, `on_accepted`, `on_disconnect`) and message handlers.
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

    async def on_accepted(self, ws):
        await ws.send_media({"type": "session.ready"})

    @handles_message("chat.message")
    async def handle_message(self, ws, payload):
        await ws.send_media({"type": "echo", "text": payload.text})


app = falcon.asgi.App()
router = WebSocketRouter()
router.add_route("/chat/{room}", ChatResource)
router.mount("/ws")
router.attach(app)  # register Falcon's responder adapter
```

- Call `mount(prefix)` before `attach(app)`. The adapter registers the mounted
  prefix and its nested paths with Falcon.
- `on_connect` runs before acceptance. Use it for validation and setup without
  sending or receiving WebSocket frames. The adapter accepts an approved
  connection, then calls `on_accepted(ws)` for initial WebSocket I/O.
- The adapter owns the production `receive_text()` loop and passes each text
  frame to schema-driven `dispatch()`. Binary frames are closed with code 1003;
  applications do not need to write a receive loop.
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

### Route template syntax

Route templates match their literal text exactly. Regular-expression
metacharacters such as `.`, `+`, brackets, and parentheses have no special
meaning in a route. A parameter written as `{name}` captures one or more
non-`/` characters; parameter names must be valid Python identifiers. A path
segment may contain one parameter or an adjacent pair at its end. Other
multiple-parameter layouts are rejected to bound regex matching work.

Braces are reserved for parameters. Empty, unmatched, nested, or doubled
braces, invalid names, duplicate parameter names, and ambiguous multiple-
parameter layouts raise `ValueError` during route or subroute registration. A
conflict between a mount-prefix parameter and a route parameter raises
`ValueError` when the router is mounted. Full routes and nested subroutes are
checked in registration order, so the first matching registration selects the
resource and its hooks.

## 3. Resource Lifecycle & State

- `on_connect(req, ws, **params) -> bool | None`
  - Validate the request and seed `self.state`; return `False` to reject the
    handshake. Do not accept the socket or perform WebSocket I/O here.
- `on_accepted(ws) -> None`
  - Runs immediately after the router accepts the socket. Register the
    connection and send any initial frames here.
- `on_disconnect(ws, close_code) -> None`
  - Clean up after an accepted connection. The router first notifies the
    `before_disconnect` hooks, then calls this method, including after an
    accepted setup or handler failure.

The router uses `falcon.WebSocketDisconnected.code` for peer disconnects. An
unexpected post-accept exception is closed with code 1011; cancellation is
reported to cleanup as 1006 and is re-raised. The production adapter supports
text frames; a binary frame is closed with 1003.

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

- Messages are decoded with `msgspec`.
- Registered `@handles_message` handlers match by payload type and take
  precedence over conventional handlers.
- Conventional handlers use `on_<snake_case(str(tag))>`:
  `tag="sendMessage"` maps to `on_send_message`, and `tag=1` maps to `on_1`.
- Messages without a matching handler fall back to
  `on_unhandled(self, ws, raw)`.
- A schema must use one kind of tag throughout: either all string tags or all
  integer tags. `msgspec` cannot decode a union that mixes them, so the
  combination is rejected with a `TypeError` when the resource class is
  defined. Note that `tag=True` asks `msgspec` to use the class name as the
  tag, which is a string.
- That check only covers the `schema` declared on the class. Assigning
  `resource.schema` afterwards bypasses it, so a mixed-tag union installed that
  way is not rejected until the first message is decoded, where it raises
  `TypeError` rather than reaching `on_unhandled`. The failure is deliberate:
  the schema is a configuration error, and routing it to `on_unhandled` would
  hide it behind a bad-frame fallback while leaving the broken schema in place.
  Fix the schema instead of handling the exception.
- The decorator supports `strict=False` to allow extra fields when required.

The stable import path is `from falcon_pachinko.utils import ValidationError`.
It is the same class as `msgspec.ValidationError`, so existing
`except msgspec.ValidationError` handlers continue to work. The dispatcher
routes validation failures to `on_unhandled`; a `ValidationError` raised inside
a handler propagates.

### Reserved lifecycle names

A peer chooses the envelope `type`, so the set of method names the dispatcher
is willing to resolve from that choice is fixed when the resource class is
created. The `on_connect`, `on_accepted`, `on_disconnect` and `on_unhandled`
callbacks are driven by the connection lifecycle and are **never**
dispatchable. An alias such as `on_bye = on_disconnect`, and a copy inherited
from a parent resource, are excluded on the same terms. The same applies to the
coroutine behind a descriptor — `@classmethod`, `@staticmethod`,
`functools.partialmethod`, `functools.singledispatchmethod`,
`functools.cached_property` or `property` — when the same function is also
aliased under a neutral name, and to the bound method or generated wrapper that
reading such an entry through the class yields, which explicit registration
passes to `add_handler`.

A tag that normalizes to a reserved name — `disconnect`, `Disconnect` and
`DISCONNECT` all reduce to `on_disconnect` — cannot select the lifecycle
callback. The explicit registry is consulted first, by exact tag string; if it
holds no handler for that string, conventional lookup is refused because the
name is reserved, so the envelope reaches `on_unhandled(self, ws, raw)` with
the original frame. So `{"type": "disconnect", "payload": 1000}` cannot invoke
`on_disconnect` — and therefore cannot unregister a socket or release
connection-scoped state — while the transport is still connected. A misspelt
tag such as `dis_connect` is not a reserved name at all: it normalizes to
`on_dis_connect`, so it is unhandled only because no handler claims that
spelling.

Registration is closed on the same terms. Registering a lifecycle callback as a
message handler, whether by `@handles_message` or `add_handler`, raises
`ReservedHandlerRegistrationError`. The decorator refuses a reserved method
name as soon as it is applied, before it validates the handler's signature, so
the error names the reserved callback rather than reporting a shape problem. An
alias or an inherited copy cannot be judged until the class exists, so those
are refused when the class is created. The reserved *tag* string itself stays
legal: a distinct method may handle `"disconnect"` provided it is not the
lifecycle callback:

```python
class ChatResource(WebSocketResource):
    @handles_message("disconnect")
    async def handle_disconnect(
        self, ws, payload: object
    ) -> None: ...  # runs for {"type": "disconnect"}; on_disconnect does not

    async def on_disconnect(
        self, ws, close_code: int
    ) -> None: ...  # connection lifecycle only
```

Schema-backed dispatch restricts tags to declared `Struct` types, but a struct
may be tagged with a reserved name; the conventional fallback refuses it there
too, so an unregistered struct tagged `"disconnect"` falls back rather than
calling through.

### Diagnostic output and raw payloads

Framework-generated validation errors, logs, trace summaries and object
representations omit payload values by default. They may include bounded
structural metadata such as field or handler names, expected types, frame kind
and length, exception class, and lifecycle phase. This keeps a malformed
authentication frame from placing a token or other payload value in ordinary
diagnostic output.

The raw values remain available to explicitly registered application hooks and
handlers. For example, `HookContext.raw` and the `on_unhandled` fallback
argument retain the original frame. `TraceEvent.payload` retains the value
recorded by the trace; a JSON receive records the decoded object, not
necessarily the original frame. Treat raw access as a trusted boundary: the
framework cannot prevent application hook or handler code from explicitly
logging raw payloads, which can disclose secrets. Decoding bytes as UTF-8 does
not sanitize the resulting text.

For trusted local debugging, opt in to a bounded sample formatter:

```python
from falcon_pachinko import DiagnosticSanitizer

sanitizer = DiagnosticSanitizer(
    extra_sensitive_keys=frozenset({"session_id", "client_secret"})
)
sample = sanitizer.format_sample({"status": "rejected", "access_token": "local-token"})
```

The sanitizer traverses only exact built-in dictionaries, lists, tuples and
scalar types. Configure `max_depth`, `max_items`, `max_string_length`, and
`max_output_length` to bound samples. Integer magnitudes are also bounded. It
detects cycles and describes bytes without decoding them. Unsupported objects
are described by safe type name without their value; non-string or unusable
mapping keys are omitted. If a value cannot be inspected safely, it is omitted.
`format_sample()` also enforces a total output-length limit, while `sanitize()`
returns a bounded tree. If its complete JSON-serialized result would exceed
`max_output_length`, `sanitize()` returns the `<budget>` omission marker.
Sensitive-key matching normalizes case and common separators, then matches
configured fragments within the key. `DEFAULT_SENSITIVE_KEYS` provides common
credential names, and `extra_sensitive_keys` extends that set. Redaction is a
defence-in-depth aid: applications may use unfamiliar names or positional
values, so omission remains the default security boundary.

## 5. Hooks

- Register global hooks on the router or per-resource hooks (`before_connect`,
  `after_connect`, `before_message`, `after_message`, etc.).
- Execution order is onion-style: global → parent → child → handler → child →
  parent → global.
- Raise from a `before_*` hook to terminate negotiation early.

## 6. Dependency Injection (How-to)

1) **Choose a factory** – Pass `resource_factory` to `WebSocketRouter`.
2) **Provide services** – Register shared services in the container.
3) **Mount and attach the router** – All route instantiation flows through
   the configured factory.

```python
from falcon_pachinko import ServiceContainer, WebSocketRouter, install

conn_mgr = install(app)
container = ServiceContainer()
container.register("conn_mgr", conn_mgr)
container.register("db", my_db_pool)

router = WebSocketRouter(resource_factory=container.create_resource)
router.add_route("/rooms/{room}", ChatResource)
router.mount("/ws")
router.attach(app)
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
# Register this manager in the same resource factory/container used by the router.
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
- Use Falcon middleware `process_startup(scope, event)` and
  `process_shutdown(scope, event)` to start and stop workers. Workers can use
  the same DI container as WebSocket resources.
- If `start()` fails while creating or scheduling a worker, it closes any
  rejected coroutine, cancels and awaits tasks already scheduled, resets the
  controller and re-raises the original error. The same controller can then be
  started again. Rollback failures are attached to the original error as notes.

```python
import falcon.asgi

from falcon_pachinko import WorkerController, install, worker

app = falcon.asgi.App()
conn_mgr = install(app)
controller = WorkerController()


@worker
async def broadcaster(*, conn_mgr):
    async for ws in conn_mgr.connections():
        await ws.send_media({"type": "ping"})


class WorkerLifecycleMiddleware:
    async def process_startup(self, scope, event):
        await controller.start(broadcaster, conn_mgr=conn_mgr)

    async def process_shutdown(self, scope, event):
        await controller.stop()


app.add_middleware(WorkerLifecycleMiddleware())
```

The `@worker` decorator is optional and marks intent only.
`WorkerController.start()` runs only the workers that the application passes to
it. An undecorated async function works in the same way.

## 10. Testing Toolkit

- **WebSocketTestClient** – Real websocket client powered by `websockets`,
  designed for integration tests; captures original payloads for assertions,
  while trace representations and `TraceEvent.summary()` show safe metadata.
- **WebSocketSimulator** – In-memory fake implementing the WebSocket protocol
  for fast unit tests.
- **Pytest fixtures** – See `tests/behaviour/*.feature` and
  `falcon_pachinko/unittests` helpers for factory utilities.
- **Live ASGI server** – `serve_asgi_app(app)` starts Falcon and its lifespan
  on an ephemeral local port for tests using `WebSocketTestClient`.

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
- Use the live ASGI server and `WebSocketTestClient` for Falcon responder,
  handshake, and lifespan behaviour. Simulator-based behavioural tests remain
  useful for focused router/resource coverage, but do not prove Falcon's live
  responder lifecycle. Direct `router.on_websocket()` calls set up and accept a
  connection, including `on_accepted`, but do not run the production receive
  loop.
- Worker tests using the `WorkerController` fixture from
  `tests/behaviour/lifespan_workers.feature`.

## 11. Reference Example

`examples/reference_app` is the acceptance example for the live Falcon ASGI
WebSocket path. Start it from the repository root with either command:

```bash
python -m examples.reference_app.server
# or
uvicorn --factory examples.reference_app.server:create_app --host 127.0.0.1 --port 8000
```

Connect to `/ws/workspaces/atlas/projects/triage/tasks` with
`x-workspace-token: seekrit` and `x-user: casey`. The route resolves through
the nested `WorkspaceResource` → `ProjectResource` → `TaskStreamResource`
chain. The `before_connect` hook validates the workspace token; an invalid
token rejects the handshake with HTTP 403 and records `auth.rejected` with
reason `invalid_token`.

After acceptance, the app registers the connection in `workspace:atlas` and
sends `session.ready`. Send a JSON `task.add` message, for example
`{"type":"task.add","task_id":"T-42","title":"Investigate"}`. The schema
decodes it into the handler, which replies with `task.added`; a lifespan worker
sends an `announcement` whose payload kind is `task_added` to the workspace.
The tested live journey then disconnects with code 1000, triggers
`before_disconnect` and `on_disconnect`, and verifies connection manager
cleanup plus the `workspace.loaded`, `project.loaded`, `session.open`,
`message.received`, `message.processed`, and `session.closed` audit events for
the same connection. See `tests/behaviour/reference_example_live.feature`. The
app factory exposes the example container as `app.reference_services`, so the
audit trail, workspace repository, and connection manager can be inspected
after a live test.
