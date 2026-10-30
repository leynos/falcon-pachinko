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
- **Falcon support range:** The requirement changes from `falcon>=4,<5` to
  `falcon>=4,<4.5`. Falcon 4.5 is unsupported until compatibility is verified
  because the WebSocket adapter uses Falcon's private `_receive()` method. Pin
  Falcon below 4.5 or downgrade an existing installation before resolving
  dependencies.
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

The `on_connect`, `on_session_start`, `on_disconnect` and `on_unhandled`
callbacks are reserved: they are driven by the connection lifecycle, and a
peer-chosen envelope `type` can no longer select them — not an alias of one,
not a copy inherited from a parent resource, and not the coroutine behind a
descriptor such as `@classmethod`, `@staticmethod`, `functools.partialmethod`,
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
  connection. Return a falsey value to reject, `True` to accept with defaults,
  or `AcceptOptions` to select a subprotocol and response headers. The router
  owns acceptance after the `after_connect` hooks. Calling `ws.accept()` in
  `on_connect` remains temporarily supported with a deprecation warning, but
  accepts immediately and changes the hook order for that compatibility path.
- `on_session_start(ws)` runs after acceptance and before frame reception. Move
  sends, receives, and active connection registration here.
- The router dispatches frames in order and attempts `on_disconnect()` once for
  each accepted session. Missing peer codes use `1006`; cancellation closes with
  `1001`; unexpected receive or handler failures close with `1011` and
  propagate after cleanup.

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

The normal Falcon router path runs `on_websocket()`, which negotiates and then
runs the accepted `WebSocketSession`. Its sequential receive loop awaits each
resource dispatch before reading another frame. Returning normally from
`on_unhandled` keeps the session open. For manual negotiation, call
`await router.negotiate(req, ws)`: it returns `None` for a rejected connection
or an accepted session that has not run `on_session_start()`. Manual dispatch
must finish the session with `await session.aclose()`. If `run()` is active in
another task, `aclose()` waits without a timeout; cancel and await that task to
bound shutdown.

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

1) **Move WebSocket work into the supported lifecycle:**

- Keep request-metadata checks and rejection in `on_connect`. Return `False`
  to reject, `True` for default acceptance, or `AcceptOptions` to choose a
  subprotocol and response headers. The router performs the one real accept.
- Remove calls to `ws.accept()`. They remain temporarily supported with a
  `DeprecationWarning`, but router acceptance is the supported path and a
  second accept raises `LifecycleError`.
- The compatibility call performs acceptance immediately inside `on_connect`,
  so `after_connect` hooks run after acceptance for that resource. A false
  decision after this call closes and finalizes the accepted session. Moving to
  a boolean or `AcceptOptions` return removes this ordering exception and
  restores `after_connect` before acceptance.
- Move sends and receives out of `on_connect` and into `on_session_start`,
  which runs after the handshake. This is the supported place for a hello frame
  or a deadline-bound authentication exchange. Before acceptance,
  `send_media()`, `receive_media()` and `receive_text()` raise `LifecycleError`
  with a pointer to `on_session_start`.
- For manual dispatch, call `await router.negotiate(req, ws)`. It returns
  `None` for a rejected connection or an accepted `WebSocketSession` that has
  not started; dispatch through `session.resource` and close it with
  `await session.aclose()` when finished. If session execution is active in
  another task, `aclose()` waits for it without a timeout; cancel and await the
  run task to bound shutdown. `on_websocket()` performs both negotiation and
  session execution for normal Falcon routing.
- Router negotiation's `WebSocketSession` is the server-side handle.
  `WebSocketTestClient.connect()` yields a `WebSocketTestSession` client
  facade. Direct imports of the older
  `falcon_pachinko.testing.client.WebSocketSession` name remain as a
  compatibility alias; prefer the distinct `WebSocketTestSession` name.
- Update code that depends on raw socket identity or
  `isinstance(ws, falcon.asgi.WebSocket)`. The supplied socket is a guarded
  forwarding wrapper with read-only `phase` and `close_code`; other public
  attributes are forwarded, but wrapper identity differs from Falcon's
  transport.
- On the supported boolean/`AcceptOptions` path, `after_connect` runs
  child → parent → global before acceptance. Its `HookContext.result` is a
  boolean view of the decision; changing it does not change the already-made
  `on_connect` decision. A rejected unaccepted socket receives no disconnect
  hooks. For accepted sessions, `before_disconnect` runs global to parent to
  child, followed exactly once by the innermost `on_disconnect`.
- Manual `HookManager` callers must pass one `HookContext` to
  `notify_before_disconnect()`, rather than separate `target`, `ws` and
  `close_code` arguments. Preserve the final error when constructing it:

  ```python
  from falcon_pachinko import HookContext, HookEvent

  await hook_manager.notify_before_disconnect(
      HookContext(
          event=HookEvent.BEFORE_DISCONNECT,
          target=resource,
          resource=None,
          ws=ws,
          close_code=close_code,
          error=error,
      )
  )
  ```

- Custom transports returned by `simulator_factory`, and other
  `WebSocketLike` implementations, must provide `send_text(message)` and
  `receive_text()`. Their `send_media` implementation must accept its media
  argument positionally (`send_media(media, /)`) to match the updated protocol.

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
- `WebSocketSimulator.pending_outbound()` was removed. Use
  `len(simulator.sent_messages)` for an immediate count, or
  `await connection.wait_for_sent_count(count)` to wait for the connection to
  record that many outbound frames.

## Common Pitfalls

- Calling `router.mount(prefix)` without registering the router on the Falcon
  app leaves requests unrouted. Follow it with `router.attach(app)` to register
  the exact and descendant paths.
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
