# Falcon-Pachinko User Guide

This guide explains how to build WebSocket applications with the composable
architecture introduced in Falcon-Pachinko. It focuses on the recommended
router/resource model, then dives into advanced patterns such as dependency
injection, shared state, custom connection manager backends, background
workers, and testing utilities.

## Requirements

Falcon-Pachinko supports `falcon>=4,<4.5`. Falcon 4.5 is not yet supported
because the WebSocket adapter uses Falcon's private `_receive()` method. See the
[migration guide](migration-guide.md#what-changed) before upgrading an
existing installation.

## 1. Core Concepts

- **WebSocketRouter** – Mountable Falcon resource that owns route definitions
  and orchestrates instantiation.
- **WebSocketResource** – Per-connection handler exposing lifecycle hooks
  (`on_connect`, `on_session_start`, `on_disconnect`) and message handlers.
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
"""Composable Falcon ASGI WebSocket chat quick-start.

Run this app with ``uvicorn quickstart:app``.
"""

import dataclasses as dc
import typing as typ

import falcon.asgi
import msgspec as ms

from falcon_pachinko import (
    WebSocketResource,
    WebSocketRouter,
    configure_raw_frame_media,
    handles_message,
)

if typ.TYPE_CHECKING:
    from falcon_pachinko.protocols import WebSocketLike


class ChatPayload(ms.Struct):
    """Typed message body for a chat message."""

    text: str


class ChatResource(WebSocketResource):
    async def on_connect(self, req, ws, room: str = "lobby") -> bool:
        self.state["room"] = room
        return True  # continue to message handling

    @handles_message("chat.message")
    async def handle_message(self, ws, payload: ChatPayload) -> None:
        # The router leaves an absent payload as None.
        if payload is None:
            return
        await ws.send_media({"type": "echo", "text": payload.text})


@dc.dataclass(frozen=True, slots=True)
class _RouterRequest:
    """Present the mount template while delegating to Falcon's request."""

    request: falcon.asgi.Request
    path: str
    path_template: str
    uri_template: str

    def __getattr__(self, name: str) -> object:
        """Delegate access to the original Falcon request."""
        return getattr(self.request, name)


class RouterAdapter:
    """Route Falcon WebSocket connections to a mounted router.

    Parameters
    ----------
    router : WebSocketRouter
        Router that matches paths beneath its mount point.
    """

    def __init__(self, router: WebSocketRouter) -> None:
        """Retain the router for incoming connections."""
        self._router = router

    async def on_websocket(
        self,
        req: falcon.asgi.Request,
        ws: falcon.asgi.WebSocket,
        rest: str = "",
    ) -> None:
        """Forward one handshake using the router's mount template.

        Parameters
        ----------
        req : falcon.asgi.Request
            Falcon request for the incoming WebSocket handshake.
        ws : falcon.asgi.WebSocket
            Falcon WebSocket for the incoming connection.
        rest : str
            Path captured below the ``/ws`` route prefix.
        """
        await self._router.on_websocket(
            _RouterRequest(
                request=req,
                path=req.path,
                path_template="/ws",
                uri_template="/ws",
            ),
            typ.cast("WebSocketLike", ws),
        )


app = falcon.asgi.App()
configure_raw_frame_media(app)
router = WebSocketRouter()
router.add_route("/chat/{room}", ChatResource)
router.add_route("/", ChatResource)
router.mount("/ws")
app.add_route("/ws", RouterAdapter(router))
app.add_route("/ws/{rest:path}", RouterAdapter(router))
```

- The router is mounted once (`router.mount("/ws")`). In Falcon, register it
  both at the exact prefix and at one descendant route ending in
  `/{pachinko_path:path}` so it receives descendant paths. The router matches
  using the concrete request path.
- `on_connect` runs before router acceptance. Return `True` to ask the router
  to accept, or return a falsey value to reject the connection.
- Each connection receives a **fresh resource instance** and a **shared state
  proxy** scoped to that connection.
- The router accepts approved connections, runs `on_session_start()`, then
  receives and dispatches frames sequentially until the session ends.

### The `add_route` signature

```python
def add_route(
    self,
    path: str,
    resource: type[WebSocketResource] | cabc.Callable[..., WebSocketResource],
    *init_args: object,
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

- `on_connect(req, ws, **params) -> bool | AcceptOptions`
  - Inspect request metadata and seed `self.state`. Return a falsey value to
    reject, `True` to accept with defaults, or `AcceptOptions` to select a
    subprotocol and response headers. The router accepts after `after_connect`
    hooks complete. Calling `ws.accept()` in this hook remains temporarily
    supported for migration but emits a deprecation warning and accepts before
    `after_connect` runs.
- `on_session_start(ws) -> None`
  - Runs after successful acceptance and before frame reception. Send a ready
    frame, register the socket with a manager, or perform other active-session
    setup here. Failure closes the accepted connection and still runs disconnect
    cleanup.
- `on_disconnect(ws, close_code) -> None`
  - The router runs `before_disconnect` hooks then attempts this callback once
    for each accepted session, including `on_session_start()` or handler
    failures. Peer close codes pass through; a missing code uses `1006`.
    Cancellation uses `1006` as cleanup metadata, sends no close frame with that
    reserved code, and propagates `CancelledError`. An `on_disconnect()` callback
    already in progress is not retried. Unexpected receive or handler failures
    close with `1011` before the original error propagates. Rejected connections
    and failed `on_connect()` calls do not run disconnect cleanup.
- For manual negotiation, call `await router.negotiate(req, ws)`. It returns
  `None` when the selected resource rejects, or an accepted `WebSocketSession`
  that has not run `on_session_start()`. Call `await session.run()` to use the
  router's ordered receive loop, or dispatch manually and finish with
  `await session.aclose()`. If `run()` is active in another task, `aclose()`
  waits without a timeout; cancel and await that task to bound shutdown.

For an attached router, `attach()` installs pass-through TEXT and BINARY
deserializers in the application's shared `ws_options.media_handlers` mapping.
For a manually mounted router, call `configure_raw_frame_media(app)` before
Falcon constructs WebSockets. Both configure all WebSocket routes on the app,
warn when replacing customized inbound handlers, and preserve the configured
outgoing serializers. Falcon's default handlers decode input when neither entry
point has enabled raw-frame handling.

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

Native Falcon sockets are wrapped in a `SessionWebSocket` proxy. Public methods
and attribute reads are forwarded, but the proxy has its own identity:
`isinstance(ws, falcon.asgi.WebSocket)` is false, and attributes assigned to
`ws` are stored on the proxy. Its `unaccepted` property retains acceptance
history, including after close, rather than mirroring Falcon's post-rejection
state. Resources and connection managers should use the supplied socket's
capabilities rather than rely on concrete type or identity checks.

Custom transports keep the existing `WebSocketLike` protocol. Transports with an
`unaccepted` property retain its authoritative state checks. Transports
without it receive the same proxy treatment: missing state is initially
unknown, and successful calls to `accept()` through the supplied socket
establish the cleanup promise. Resource-owned acceptance is tracked even if
`on_connect` then returns a falsey result or raises.

| Session ending                                    | `close_code` passed to `on_disconnect`                                                               |
| ------------------------------------------------- | ---------------------------------------------------------------------------------------------------- |
| Client sends a close frame                        | The close code Falcon reports                                                                        |
| Resource or server initiates a close              | Falcon's reported disconnect code, when available; the requested outbound code is sent to the client |
| Transport disappears without a close frame        | `1006` (abnormal closure)                                                                            |
| Task is cancelled                                 | `1006` cleanup metadata; no close frame is sent with this reserved code                              |
| Handler or setup fails with an ordinary exception | `1011` (internal error); the router attempts to close with this code                                 |

Falcon's `WebSocketDisconnected` is a transport ending even when a resource
observes it while sending or during accepted setup; its reported code is
retained. Cancellation after disconnect cleanup begins does not rewrite the
close code already selected.

The router runs `before_disconnect` before `on_disconnect`, then marks the
session closed. Cancellation uses `1006` as cleanup metadata and sends no close
frame with that reserved code. An `on_disconnect()` callback interrupted while
already in progress is not retried. Once disconnect begins, receive and
dispatch hooks do not run for later frames. Cleanup runs in a shielded task and
has no timeout, so `on_disconnect` must terminate. A cleanup exception does not
replace a lone session exception; when both are ordinary exceptions, they are
raised in an `ExceptionGroup` with the session exception first. A single
failure is raised unchanged. Cancellation remains a bare `CancelledError`;
cleanup failures that cannot be grouped with it are reported to the event
loop's exception handler.

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
- The router dispatches frames sequentially on the accepted connection. It
  does not start a task per frame, so one handler completes before the next
  frame is dispatched. Returning normally from `on_unhandled` keeps the session
  open; application code can call `await ws.close(code=...)` when its policy
  requires closure.

Falcon's default WebSocket media handlers may decode frames before
`receive_media()` returns them. Raw-frame media setup is required when dispatch
needs the exact TEXT or BINARY payload. When schema dispatch needs the original
TEXT or BINARY payload, configure the app before constructing WebSocket
connections. `router.attach()` does this automatically; use
`configure_raw_frame_media(app)` for manually mounted routers or other Falcon
WebSocket routes:

```python
from falcon_pachinko import configure_raw_frame_media

configure_raw_frame_media(app)
```

This is an app-wide setting, so it also affects ordinary Falcon WebSocket
routes in the same application. Both setup paths preserve incoming `str` and
`bytes` payloads while retaining the app's configured outgoing TEXT and BINARY
serializers. The standalone `RawTextMediaHandler` and `RawBinaryMediaHandler`
continue to encode JSON and MessagePack respectively when explicitly installed
as handlers.

The stable import path is `from falcon_pachinko.utils import ValidationError`.
It is the same class as `msgspec.ValidationError`, so existing
`except msgspec.ValidationError` handlers continue to work. The dispatcher
routes validation failures to `on_unhandled`; a `ValidationError` raised inside
a handler propagates.

Native text and binary frames reach the dispatcher unchanged. Malformed JSON
can therefore reach `on_unhandled(self, ws, raw)` without Falcon's media
decoder ending the connection first. Falcon's public media sending methods
retain their usual arguments, including `send_media(media=...)`.

### Reserved lifecycle names

A peer chooses the envelope `type`, so the set of method names the dispatcher
is willing to resolve from that choice is fixed when the resource class is
created. The `on_connect`, `on_session_start`, `on_disconnect` and
`on_unhandled` callbacks are driven by the connection lifecycle and are
**never** dispatchable. An alias such as `on_bye = on_disconnect`, and a copy
inherited from a parent resource, are excluded on the same terms. The same
applies to the coroutine behind a descriptor — `@classmethod`, `@staticmethod`,
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

### Clustered deployments

Clustering uses `ClusterFanout` alongside a separate local
`WebSocketConnectionManager`. Each process keeps and sends to its own live
sockets. A `FanoutTransport` carries room leases and serialized `FanoutMessage`
envelopes; it never carries a socket. An envelope is JSON encoded as bytes and
contains `message_id`, `origin_node`, `room`, `payload` and `exclude`. The
transport exchanges string identifiers, floating-point lease durations and byte
envelopes; `listen()` yields bytes and `members()` returns node identifiers.

Give each node a cluster-unique node ID and each connection a cluster-unique
connection ID. Register and group connections through the coordinator, then
publish application payloads through it:

```python
await fanout.add_connection(connection_id, ws)
await fanout.join_room(connection_id, room)
await fanout.publish(room, {"text": "hello"})
await fanout.remove_connection(connection_id)  # on disconnect
```

Start `fanout.run` with `WorkerController` in the application lifespan and stop
the controller during shutdown. The router keeps accepted sockets open,
dispatches incoming frames and calls `on_disconnect` when the session ends.
Each node subscribes while it has local members in a room. It removes the
subscription when the last local member leaves, releases its memberships on
graceful shutdown, and renews leases with a heartbeat (by default, every third
of `lease_ttl`). If a process crashes, its remote membership expires after
`lease_ttl` without a heartbeat.

Fan-out is best effort and not durable: nodes that are down or whose leases
have expired do not receive replayed messages. Per-socket ordering depends on
the transport preserving per-publisher, per-node order; there is no global
ordering guarantee. At-least-once transports can deliver duplicates, and the
coordinator does not deduplicate them. `message_id` is available in the
serialized envelope to applications, but is not added to the WebSocket payload.
A transport publish failure is raised without retry; individual local send
failures do not stop the listener, remove a connection or roll back the publish.
`send_timeout` defaults to 5.0 seconds; any numeric value must be finite and
positive. Pass `None` explicitly for unbounded sends; a blocked send can stall
the sequential listener and delay delivery to other rooms. Existing local
snapshots continue to skip stale local members.

`InMemoryFanoutTransport` is provided for tests. A production Redis or RabbitMQ
adapter must implement only `FanoutTransport` and is outside the scope of this
release. Single-process applications can use `WebSocketConnectionManager` alone
and need no broker.

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

```bash
python -m examples.reference_app.server
# or
uvicorn --factory examples.reference_app.server:create_app --host 127.0.0.1 --port 8000
```

Connect to `ws://127.0.0.1:8000/ws/workspaces/atlas/projects/triage/tasks` with
headers `x-workspace-token: seekrit` and `x-user: casey`. After Falcon accepts
the connection, `on_session_start` registers the socket in `workspace:atlas`
and sends `session.ready`. Send a JSON text message such as:

```json
{"type":"task.add","task_id":"T-42","title":"Investigate live WebSockets"}
```

The schema dispatcher invokes the task handler, which replies with `task.added`
and publishes a worker-feed event. The lifespan worker broadcasts an
`announcement` with `kind: task_added` to the workspace room. On a normal close
with code `1000`, `before_disconnect` runs before `on_disconnect`, which
removes the connection and records `session.closed`. The app exposes its
container as `app.reference_services` for access to the audit trail,
repository, and connection manager.

The live pytest-bdd feature verifies the full journey, including
`workspace.loaded`, `project.loaded`, `session.open`, `message.received`,
`message.processed`, and `session.closed` for the same connection ID. It also
verifies that an invalid token rejects the handshake and records
`auth.rejected` with reason `invalid_token`. Run it with:

```bash
uv run pytest -q tests/behaviour/test_reference_example_live_steps.py
```

The example router accepts the socket and owns ordered frame reception and
dispatch through its session lifecycle. No application receive loop or direct
`WebSocketResource.dispatch()` call is needed. Use the example as a blueprint.
Adapt its authentication, services, and worker policy to the target application.
