# Falcon-Pachinko User Guide

This guide explains how to build WebSocket applications with the composable
architecture introduced in Falcon-Pachinko. It focuses on the recommended
router/resource model, then dives into advanced patterns such as dependency
injection, shared state, custom connection manager backends, background
workers, and testing utilities.

## Installation

The distribution is named `falcon-pachinko`; Python code imports it as
`falcon_pachinko`. Install it with pip or add it to a uv-managed project:

```bash
python -m pip install --pre falcon-pachinko
uv add --prerelease allow falcon-pachinko
```

The current alpha is a pre-release. To install pre-releases, opt in explicitly
with `python -m pip install --pre falcon-pachinko` or
`uv add --prerelease allow falcon-pachinko`.

Install the `examples` extra for the example application's dependencies or the
`testing` extra for the package's testing utilities:

```bash
python -m pip install --pre 'falcon-pachinko[examples]'
python -m pip install --pre 'falcon-pachinko[testing]'
uv add --prerelease allow 'falcon-pachinko[examples]'
uv add --prerelease allow 'falcon-pachinko[testing]'
```

## 1. Core Concepts

- **WebSocketRouter** – Mountable Falcon resource that owns route definitions
  and orchestrates instantiation.
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
router = WebSocketRouter(receive_loop=True)
router.add_route("/chat/{room}", ChatResource)
router.add_route("/", ChatResource)
router.mount("/ws")
app.add_route("/ws", RouterAdapter(router))
app.add_route("/ws/{rest:path}", RouterAdapter(router))
```

- Falcon matches a connection to `/ws` against the exact mount route, or a
  descendant path against `/ws/{rest:path}`, then forwards the connection to
  `RouterAdapter`. The adapter presents the `/ws` mount template to the router
  and delegates other request attributes and methods to Falcon's request. The
  router then matches `/ws/chat/{room}` against its mounted routes.
- `configure_raw_frame_media(app)` preserves inbound TEXT and BINARY frames
  for schema dispatch when the router is mounted manually.
- Each connection receives a **fresh resource instance** and a **shared state
  proxy** scoped to that connection.
- The example opts into a router-owned receive-and-dispatch session with
  `receive_loop=True`. The default is `False`, which returns after acceptance
  and leaves receives and cleanup to the application.
- When `payload` is present and valid, the `ChatPayload` annotation makes the
  router pass a `ChatPayload` instance to the handler. When `payload` is
  absent, the typed handler receives `None`; this example ignores it.
- An unannotated handler receives the decoded JSON value directly, such as a
  `dict` for an object, a list for an array, or `None` when the payload is
  absent.

Save the Python snippet as `quickstart.py`. An ASGI server is required to serve
the WebSocket connection; Uvicorn is included in the `examples` extra or the
`dev` dependency group.

```sh
uvicorn quickstart:app
```

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
  - Inspect headers and seed `self.state`; return `False` to reject the
    connection. The router runs `after_connect` hooks before it calls
    `accept()`. With `receive_loop=True`, it then begins receiving frames; with
    the default `False`, the application owns reception. If `on_connect()` sends
    a frame itself, call `await ws.accept()` first.
- `on_accepted(ws) -> None`
  - Runs immediately after Falcon accepts an allowed connection. Put
    accept-dependent I/O here, such as registering the socket with a connection
    manager or sending an initial frame. Disconnect cleanup also covers
    failures in this hook. It does not add a separate hook event.
- `on_disconnect(ws, close_code) -> None`
  - The router attempts this callback once when it owns cleanup after
    `on_connect` permits the connection. With `receive_loop=True`, it also
    owns cleanup after peer disconnect, cancellation, or receive failure. A
    failure during `after_connect`, `accept`, or `on_accepted` is cleaned up
    even when `receive_loop=False`. Peer disconnects use
    `WebSocketDisconnected.code`; an unavailable code and cancellation use
    `1006` as cleanup metadata. Cancellation propagates without sending a close
    frame with reserved code `1006`. Unexpected receive or handler failures
    use `1011` and propagate the original exception while the transport stays
    open. If the transport already closed without a reported code, cleanup uses
    `1006` and skips another close attempt. An ordinary
    exception from `on_accepted` uses `1011` while the transport remains open;
    a closed transport without a reported code uses `1006`. A rejection that
    remains unaccepted does not run cleanup. If
    `on_connect` accepts the socket before failing, cleanup still runs; an
    unaccepted `on_connect` failure must roll back any partial registration.
    With successful setup and `receive_loop=False`, the application owns
    receive and cleanup.
- With `receive_loop=True`, the router passes values returned by
  `ws.receive_media()` to `WebSocketResource.dispatch()` in order until the
  peer disconnects. An attached router configures raw media handlers so the
  dispatcher receives the original `str` or `bytes` frame.

For an attached router, `attach()` installs pass-through TEXT and BINARY
deserializers in the application's shared `ws_options.media_handlers` mapping.
For a manually mounted router, call `configure_raw_frame_media(app)` before
Falcon constructs WebSockets. Both configure all WebSocket routes on the app,
warn when replacing customized inbound handlers, and preserve the configured
outgoing serializers. Falcon's default handlers decode input when neither entry
point has enabled raw-frame handling.

For either receive mode, native Falcon sockets passed to lifecycle hooks,
message hooks, and handlers such as `on_unhandled` use a forwarding adapter
that implements `WebSocketLike`. This means
`isinstance(ws, falcon.asgi.WebSocket)` is false; attributes the adapter does
not define are forwarded to Falcon's socket. With `receive_loop=False`, the
application receives this adapter during setup and owns later reception and
cleanup after setup succeeds.

The effective close code passed to `on_disconnect` follows this table:

| Session ending                                 | Code passed to `on_disconnect`                                          |
| ---------------------------------------------- | ----------------------------------------------------------------------- |
| Client sends a close frame                     | Falcon's code, or `1006` if unavailable                                 |
| Handler or server calls `close(code)`          | Recorded code for a successful close; otherwise Falcon's code or `1006` |
| Transport loss without a close frame           | `1006`                                                                  |
| Task cancellation                              | `1006`; no close frame with this reserved code is sent                  |
| Approval succeeds but acceptance does not      | `1006`; no WebSocket close frame can be sent                            |
| Failure after acceptance on an open socket     | `1011`; the router attempts a close                                     |
| Handler failure after code-less transport loss | `1006`; the transport is already closed                                 |

After a successful `close(code)`, the adapter records Falcon's reported close
code when available, or the requested code when the transport exposes no code.
That recorded value takes precedence over later disconnect metadata. For other
endings, Falcon's reported code is used, with `1006` when no code is available.
Setup failures before the cleanup promise point do not run `on_disconnect`;
failures after approval or acceptance do. An approved but unaccepted connection
is cleaned up with `1006`, since the handshake has no established WebSocket on
which to send a close frame. A previously reported socket close code is
retained if the handler then fails. If the transport is already closed without
a code, cleanup uses `1006` and does not try to close it again. The router runs
`before_disconnect` before the callback and guards both against later receive
dispatch. Cleanup runs in a shielded task and has no timeout, so
`on_disconnect` must terminate. A lone failure propagates unchanged; multiple
ordinary failures form a primary-first `ExceptionGroup`. Cancellation remains a
bare `CancelledError`, and cleanup errors suppressed by cancellation are sent
to the event loop's exception handler.

`resource.has_router_session` is true while the router owns lifecycle work,
including connection setup. Use it to leave accepted-session membership removal
to `on_disconnect`; it does not itself establish the cleanup promise. That
promise begins only after `on_connect` approves or the socket is accepted.

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
- With `receive_loop=True`, the router dispatches frames sequentially on the
  accepted connection. It does not start a task per frame, so one handler
  completes before the next frame is dispatched. Returning normally from
  `on_unhandled` keeps the session open; application code can call
  `await ws.close(code=...)` when its policy requires closure.

Falcon's default WebSocket media handlers may decode frames before
`receive_media()` returns them; `receive_loop=True` alone does not guarantee
that dispatch receives the exact raw frame. When schema dispatch needs the
original TEXT or BINARY payload, configure the app before constructing
WebSocket connections. `router.attach()` does this automatically; use
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

### Reserved lifecycle names

A peer chooses the envelope `type`, so the set of method names the dispatcher
is willing to resolve from that choice is fixed when the resource class is
created. The `on_connect`, `on_disconnect` and `on_unhandled` callbacks are
driven by the connection lifecycle and are **never** dispatchable. An alias
such as `on_bye = on_disconnect`, and a copy inherited from a parent resource,
are excluded on the same terms. The same applies to the coroutine behind a
descriptor — `@classmethod`, `@staticmethod`, `functools.partialmethod`,
`functools.singledispatchmethod`, `functools.cached_property` or `property` —
when the same function is also aliased under a neutral name, and to the bound
method or generated wrapper that reading such an entry through the class
yields, which explicit registration passes to `add_handler`.

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
3) **Attach the router** – All route instantiation flows through the configured
   factory.

```python
import falcon.asgi

from falcon_pachinko import ServiceContainer, WebSocketRouter, install

app = falcon.asgi.App()
conn_mgr = install(app)
container = ServiceContainer()
container.register("conn_mgr", conn_mgr)
container.register("db", my_db_pool)

router = WebSocketRouter(resource_factory=container.create_resource)
router.add_route("/rooms/{room}", ChatResource)
router.mount("/ws")
router.attach(app)
```

Configure the Falcon application and its services before calling `attach()`.
`mount()` compiles the router's paths; `attach()` registers them on the real
Falcon app and installs raw-frame media handlers.

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
implementation. Use a custom backend for alternative local storage or
instrumentation. The registry owns live `WebSocketLike` objects in one process;
it never serializes, proxies or shares those sockets across processes.

```python
from falcon_pachinko.websocket import ConnectionBackend, WebSocketConnectionManager
from falcon_pachinko import ServiceContainer, WebSocketRouter


class InstrumentedBackend(ConnectionBackend): ...


conn_mgr = WebSocketConnectionManager(backend=InstrumentedBackend(...))
container = ServiceContainer()
container.register("conn_mgr", conn_mgr)
router = WebSocketRouter(resource_factory=container.create_resource)
```

Pass a custom manager through the resource factory. `install(app)` creates a
default manager and returns it; Falcon's slotted ASGI app does not expose it as
`app.ws_connection_manager`.

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
the controller during shutdown. Mount the router with
`WebSocketRouter(receive_loop=True)` so it keeps accepted sockets open,
dispatches incoming frames and calls `on_disconnect` when the session ends.
This receive loop is opt-in; the default returns after acceptance and leaves
subsequent receives and cleanup to the application after successful setup. Each
node subscribes while it has local members in a room. It removes the
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
- Add middleware with Falcon's `process_startup(scope, event)` and
  `process_shutdown(scope, event)` methods to wire startup and shutdown;
  workers can depend on the same DI container used for WebSocket resources.
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
        del scope, event
        await controller.start(broadcaster, conn_mgr=conn_mgr)

    async def process_shutdown(self, scope, event):
        del scope, event
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
- **Live server harness** – `LiveWebSocketServer` serves a real Falcon ASGI app
  on an ephemeral localhost port. The `live_websocket_server` pytest fixture
  provides `start(app)` and a loop-bound `run(awaitable, timeout=...)` helper.
  Connect with `server.connect(path)` to use `WebSocketTestClient` over a real
  RFC WebSocket connection and have the fixture track the client for teardown.
- **Pytest fixtures** – See `tests/behaviour/*.feature` and
  `falcon_pachinko/unittests` helpers for factory utilities.

An async test can use the server directly:

```python
import pytest

from falcon_pachinko.testing import LiveWebSocketServer


@pytest.mark.asyncio
async def test_ping_reply() -> None:
    app = build_app()

    async with LiveWebSocketServer(app) as server:
        async with server.connect("/ws/chat") as session:
            await session.send_json({"type": "ping"})
            assert await session.receive_json() == {"type": "pong"}

        assert server.pop_errors() == ()
```

Entering the server waits for ASGI lifespan startup and listener readiness.
`server.connect()` tracks each client, and server teardown closes any clients
the test leaves open. `server.errors` returns a read-only snapshot of captured
failures; `server.pop_errors()` returns and acknowledges them. The harness
captures exceptions handled by Falcon, exceptions escaping the ASGI callable,
and server-task failures. Unacknowledged failures are grouped in
`LiveServerError` when the test body completed without an exception. If the
test body is already raising, the harness adds captured failures as notes to
that exception so the original failure stays primary.

When `capture_errors=True`, use a fresh Falcon app for each server instance:
the capture handler remains registered on the app after server shutdown.

A startup timeout or ordinary startup exception raises
`LiveServerStartupError`. A shutdown deadline is recorded as
`LiveServerShutdownError` and reported during teardown using the same error
handling. The harness installs its Falcon `Exception` capture handler when the
server starts, replacing a handler registered earlier. If the application needs
a custom handler, register it after startup and have it call
`server.record_error(ex)`; otherwise Falcon may consume the exception before
the harness can report it. For synchronous tests, use the
`live_websocket_server` fixture's `start(app)` and
`run(awaitable, timeout=...)` methods instead. See the
[developer guide's WebSocket lifecycle guidance](developers-guide.md#websocket-lifecycle-acceptance-tests)
for the fixture and custom-handler details.

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
- Use `pytest-bdd` live-server scenarios as the required acceptance path for
  every WebSocket lifecycle feature. These prove that real Falcon ASGI
  responders keep a socket open and exchange frames over a real connection.
- Keep simulator tests for fast routing, hooks, DI, and connection-manager
  coverage; they complement the live suite and do not replace it.
- The `lifespan_workers.feature` BDD scenarios verify workers running during
  application lifespan; the feature file is not a fixture.

## 11. Reference Example

`examples/reference_app` is a live Falcon ASGI demonstration of nested
workspace, project, and task resources; header authentication; schema-driven
message handling; dependency injection; hooks; a lifespan worker; and
connection-manager cleanup.

Start it from the repository root on `127.0.0.1:8000` using either command:

```bash
python -m examples.reference_app.server
# or
uvicorn --factory examples.reference_app.server:create_app --host 127.0.0.1 --port 8000
```

Connect to `ws://127.0.0.1:8000/ws/workspaces/atlas/projects/triage/tasks` with
headers `x-workspace-token: seekrit` and `x-user: casey`. After Falcon accepts
the connection, `on_accepted` registers the socket in `workspace:atlas` and
sends `session.ready`. Send a JSON text message such as:

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

The example's router uses `receive_loop=True`, so the library accepts the
socket and owns ordered frame reception and dispatch. No application receive
loop or direct `WebSocketResource.dispatch()` call is needed. Use the example
as a blueprint. Adapt its authentication, services, and worker policy to the
target application.
