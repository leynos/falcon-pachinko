# Migration Guide: Pre-release API → Composable Router

This guide helps existing users move from the early `add_websocket_route`
workflow to the composable `WebSocketRouter` architecture.

## What Changed

- **Mountable router** replaces per-route registration on the Falcon app.
- **Nested resources** allow hierarchical composition with shared per-connection
  state.
- **Schema-first dispatch** via `msgspec` tagged unions and
  `@handles_message("tag")`.
- **Router-level dependency injection** through `resource_factory`.
- **Pluggable connection manager backends** enable alternative local storage;
  see [Clustered deployments](users-guide.md#clustered-deployments) for
  distributed room fan-out.
- **Lifespan-managed workers** supersede `add_websocket_worker`.

## Diagnostic compatibility

Framework-generated diagnostics now omit payload values by default. Strict
unknown-field errors report bounded field names and expected schema metadata;
they no longer include a truncated payload prefix. Setting
`include_payload=True` routes the sample through `DiagnosticSanitizer`; pass a
custom `sanitizer` to supply application-specific sensitive keys or bounds.
Redacted samples are bounded and intended for trusted local debugging.
Conventional-handler DEBUG messages retain their lazy format but now include
only a bounded handler name and exception class.

`HookContext`, `HandlerInvocationContext`, and `TraceEvent` keep their fields,
constructors, slots, equality behaviour and operational payload values, but
their representations now show safe metadata instead of raw or decoded values.
`TraceEvent.summary()` is new and returns the same kind of metadata. Code that
relied on payload values appearing in `repr()` must read the explicit payload
field at a trusted boundary.

Client session `receive_json()` continues to raise `RuntimeError` with the
`Failed to decode JSON payload` prefix. Its message now reports frame kind and
length, optional expected type, and decoder exception class. The exception has
no vendor cause or context. Simulator and harness JSON decoding continue to
raise `msgspec.ValidationError` for validation failures and
`msgspec.DecodeError` for other decode failures, using fresh errors without
vendor text or chains. Unsupported frame types raise `TypeError` with only the
safe type name. A text frame that cannot be encoded as UTF-8 raises a fresh
`UnicodeError` without frame contents or original exception text; binary-frame
failures and `msgspec` JSON decode failures retain the behaviour described
above.

Close failures still propagate the original close exception. Their trace entry
now records the exception class name instead of its message; caller-supplied
close `code` and `reason` fields retain their values.

## Schema tag kinds

A `schema` must now use one kind of tag throughout: either all string tags or
all integer tags. `msgspec` cannot decode a union that mixes them, so a
resource whose class-defined `schema` mixes the two now fails at class
definition with a `TypeError` instead of failing on the first message. This is
the only new rejection; single-kind schemas are unaffected.

`tag=True` asks `msgspec` to use the class name as the tag, so it contributes a
*string* tag and cannot be combined with integer-tagged members. Mixed-tag
schemas raised a defensive error before this release, so they were never
functional: correct the tags rather than expecting the previous behaviour.

A `schema` assigned after class creation still bypasses the class-creation
check. There the configuration `TypeError` propagates during dispatch instead
of reaching `on_unhandled`; it is deliberately not converted into a bad-frame
fallback, which would disguise a broken schema as a malformed message.

## Reserved lifecycle handlers

The `on_connect`, `on_disconnect` and `on_unhandled` callbacks are reserved:
they are driven by the connection lifecycle, and a peer-chosen envelope `type`
can no longer select them — not an alias of one, not a copy inherited from a
parent resource, and not the coroutine behind a descriptor such as
`@classmethod`, `@staticmethod`, `functools.partialmethod`,
`functools.singledispatchmethod`, `functools.cached_property` or `property`. A
tag that normalizes to a reserved name, such as `disconnect`, no longer
resolves; the frame reaches `on_unhandled` with the original frame — its
`str | bytes` message argument, not a decoded payload.

Registering such a callable as a message handler now raises
`ReservedHandlerRegistrationError`, whether through `@handles_message` or
`add_handler`.

Rename a message handler whose name collides with a lifecycle callback, or keep
the tag and register a distinct handler for it explicitly:

```python
class ChatResource(WebSocketResource):
    @handles_message("disconnect")
    async def handle_disconnect(self, ws, payload: object) -> None: ...
```

The reserved tag string itself stays legal for a distinct method; only the
lifecycle callback is refused. See
[Reserved lifecycle names](users-guide.md#reserved-lifecycle-names) in the user
guide for the exact rules.

## Step-by-Step Migration

1) **Install websocket support:**

```python
import falcon.asgi
from falcon_pachinko import websocket

app = falcon.asgi.App()
conn_mgr = websocket.install(app)
```

`websocket.install(app)` initializes the app's connection manager and legacy
WebSocket compatibility helpers. Keep the returned manager in application
state: Falcon's slotted ASGI app does not expose it as
`app.ws_connection_manager`. `router.attach()` registers the composable router
with Falcon. Use a Falcon ASGI application for WebSocket routes.

1) **Replace `add_websocket_route` with a router:**

```python
from falcon_pachinko import WebSocketRouter, WebSocketResource


class ChatResource(WebSocketResource):
    pass


router = WebSocketRouter()
router.add_route("/chat/{room}", ChatResource, history_size=100)
router.mount("/ws")
router.attach(app)
```

- Route paths are **relative to the router mount point**. `mount()` compiles
  the prefix and `attach()` registers the exact and descendant Falcon routes.
  The compatibility form `router.attach(app, "/ws")` also mounts and registers
  the router.
- Resource initializer arguments moved from `args=`/`kwargs=` parameters to
  `*init_args`/`**init_kwargs` positional and keyword capture. Before:
  `router.add_route(path, Resource, args=(1,), kwargs={"n": 2})`; after:
  `router.add_route(path, Resource, 1, n=2)`. The `name` keyword is reserved
  for the route name and is never forwarded to the resource initializer.

1) **Adopt the connection acceptance and session lifecycle:**

- `on_connect(req, ws, **params)` decides whether the router accepts the
  connection. Return `False` to reject it; returning `True` allows the router
  to accept it after the hook. If the hook sends a frame, call
  `await ws.accept()` first.
- `on_accepted(ws)` runs immediately after acceptance. Move I/O that requires an
  accepted connection, such as connection-manager registration or an initial
  send, out of `on_connect()` and into this hook.
- Rename any existing `on_accepted` message handler before upgrading. This name
  is reserved for the lifecycle callback, so handler registration will reject
  it and the router would call a legacy handler with an incompatible signature.
- Set `receive_loop=True` for router-owned, ordered frame reception and
  dispatch. Its default is `False`, which returns after successful acceptance
  and leaves subsequent receives and cleanup to the application.
- `on_disconnect(ws, close_code)` runs once when the router owns cleanup after
  `on_connect` permits the connection. With `receive_loop=True`, the router
  owns cleanup after peer disconnect, cancellation, and receive failure.
  Failures in `after_connect`, `accept`, or `on_accepted` are cleaned up even
  when `receive_loop=False`. Peer closure uses `WebSocketDisconnected.code`; a
  missing code and cancellation use `1006` as callback metadata. Cancellation
  propagates without sending a close frame with reserved code `1006`.
  Unexpected receive or handler failures use `1011` while the transport is
  open, then propagate. If the transport is already closed without a reported
  code, cleanup uses `1006` and does not attempt another close.
- In either receive mode, lifecycle hooks and handlers receive the
  `SessionWebSocket` forwarding adapter around native Falcon sockets. It
  forwards socket operations and attributes, but
  `isinstance(ws, falcon.asgi.WebSocket)` is false. With `receive_loop=False`,
  the application receives this adapter during setup and owns later reception
  and cleanup after successful setup.
- The cleanup promise begins when `on_connect` approves or the socket is
  accepted. A rejection that remains unaccepted and an `on_connect` failure
  before approval or acceptance do not call `on_disconnect`; roll back any
  partial registration in those paths. Once setup crosses that boundary,
  failures still call `on_disconnect` once.
- An exception from `on_accepted` uses close code `1011` while the transport is
  open. If the transport has already closed without a reported code, cleanup
  receives `1006` and skips another close.

`router.attach()` also enables raw TEXT and BINARY input for all WebSocket
routes on the app, preserving its outgoing serializers. For manually mounted
routers, call `configure_raw_frame_media(app)` before Falcon constructs any
WebSocket. Either entry point warns when replacing customized inbound handlers;
Falcon's default decoding remains in effect if neither is used.

1) **Adopt resource composition and state sharing:**

- Move nested paths into `add_subroute` calls inside the parent resource.
- Use `self.state` (provided automatically) to share connection-scoped data.
- Override `get_child_context()` to inject custom state stores when needed.

1) **Switch to schema-driven dispatch:**

- Define a `msgspec` union `schema` on the resource.
- Register handlers with `@handles_message("tag")` or `on_tag` methods.
- Replace legacy `on_message` fallbacks with `on_unhandled`.

Set `WebSocketRouter(receive_loop=True)` to have the router accept the
connection, call `on_accepted`, and own ordered frame reception and dispatch
until disconnect. In that mode, remove application-owned receive loops and
direct calls to `WebSocketResource.dispatch()`. Call `await ws.accept()` in
`on_connect()` only when sending a frame from that hook. Keep message handling
in resource handlers and `on_unhandled`; frames are dispatched sequentially,
and returning normally from `on_unhandled` keeps the session open. The router
runs `before_disconnect` before attempting `on_disconnect` once. A missing peer
close code or a failure before acceptance completes uses `1006` for cleanup.
After acceptance, an ordinary exception from `on_accepted` uses `1011` while
the transport remains open; a closed transport without a reported code uses
`1006` and is not closed again. Cancellation uses `1006` as cleanup metadata
and propagates without sending a close frame with the reserved code. Unexpected
receive or handler failures use `1011` and propagate the original exception
while the transport remains open. If transport loss has already closed the
socket without a code, cleanup uses `1006` and skips the close attempt. The
default `receive_loop=False` returns after successful acceptance, leaving
subsequent frame reception and cleanup to the application.

1) **Wire dependency injection via `resource_factory`:**

```python
from falcon_pachinko import ServiceContainer, WebSocketRouter, install

container = ServiceContainer()
conn_mgr = install(app)
container.register("conn_mgr", conn_mgr)
router = WebSocketRouter(resource_factory=container.create_resource)
```

- Tests can supply alternate factories (see
  `falcon_pachinko.unittests.resource_factories.resource_factory`).

1) **Update connection manager usage:**

- Instantiate `WebSocketConnectionManager` with a backend when needed:

```python
from falcon_pachinko import ServiceContainer, WebSocketRouter
from falcon_pachinko.websocket import WebSocketConnectionManager, MyBackend

conn_mgr = WebSocketConnectionManager(backend=MyBackend(...))
container = ServiceContainer()
container.register("conn_mgr", conn_mgr)
router = WebSocketRouter(resource_factory=container.create_resource)
```

Use a custom manager through resource injection. `install(app)` creates and
returns the default manager; Falcon's slotted ASGI app does not expose it as
`app.ws_connection_manager`.

- `broadcast_to_room`, `connections`, and `send_to_connection` are now `async`
  and propagate errors directly.

1) **Move background tasks to lifespan workers:**

- Replace `add_websocket_worker` with `WorkerController` and Falcon middleware
  `process_startup(scope, event)` / `process_shutdown(scope, event)` methods to
  start and stop workers.

### Route Template Compatibility

Before an upgrade, applications should audit configured templates whose literal
path text contains regular-expression metacharacters such as `.`, `+`, `?`,
`*`, brackets, parentheses, `^`, `$`, or `|`. Route matching now treats text
outside placeholders as literal, so a route containing `child.v1` matches that
exact text rather than regex alternatives such as `child/v1`. Applications that
relied on regex expansion should update their configuration; arbitrary
regex-shaped templates were not a supported route syntax.

Placeholders use the form `{name}`, where `name` is a unique Python identifier.
Each captures one or more non-slash characters. A path segment can contain one
placeholder or a terminal adjacent pair such as `{left}{right}`; other
multi-placeholder layouts in one segment are rejected as ambiguous. Braces are
reserved for placeholders, so unmatched or nested braces are invalid. Invalid
names, repeated names, ambiguous layouts, and a placeholder name repeated
between a mount prefix and its route raise `ValueError` during route
registration or mounting, before the route can handle a connection.

## Testing Checklist

- **Unit tests:** cover resource constructors, state handling, and any custom
  connection backend logic.
- **Live acceptance tests:** for each WebSocket lifecycle feature, add a
  `pytest-bdd` scenario using `live_websocket_server` and `WebSocketTestClient`
  against a Falcon ASGI app configured with `router.attach()`. This verifies
  the real responder and socket lifecycle.
- **Simulator tests:** keep `WebSocketSimulator` coverage for fast unit and
  behavioural checks of routing, hooks, dependency injection, and resource
  logic. Simulator tests complement the live acceptance path; they do not
  replace it.

## Common Pitfalls

- Calling `router.mount(prefix)` without registering the router on the Falcon
  app leaves requests unrouted. Follow it with `router.attach(app)` to register
  the exact and descendant paths.
- When registering Falcon routes manually, use a terminal `{name:path}` route
  for descendants. The converter name may be any valid field name; prefer
  `router.attach(app, prefix)` when manual registration is not required.
- Custom backends must implement *all* methods from `ConnectionBackend`,
  raising `ValueError` on duplicate IDs and ignoring stale room entries in
  `snapshot` or documenting alternative semantics.
- When migrating message handlers, ensure schema tags match incoming payloads;
  extra fields are rejected unless `strict=False` is used on `@handles_message`.
- Keep every member of a `schema` on the same tag kind. Mixing string and
  integer tags now raises `TypeError` at class definition rather than failing
  on the first frame; see *Schema tag kinds* above.
- Import `ValidationError` from `falcon_pachinko.utils` when handling payload
  validation failures. It is the same class as `msgspec.ValidationError`, so
  existing handlers remain compatible. Decode failures still reach
  `on_unhandled`, while errors raised inside a handler propagate.

## Done?

- Remove deprecated calls to
  `app.add_websocket_route`/`create_websocket_resource`.
- Update documentation links to point at `docs/users-guide.md`.
- Re-run `make check-fmt`, `make typecheck`, `make lint`, and `make test` to
  confirm the upgraded codebase passes all gates.
