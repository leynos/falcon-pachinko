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
- **Pluggable connection manager backends** enable distributed storage.
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

The `on_connect`, `on_accepted`, `on_disconnect` and `on_unhandled` callbacks
are reserved: they are driven by the connection lifecycle, and a peer-chosen
envelope `type` can no longer select them — not an alias of one, not a copy
inherited from a parent resource, and not the coroutine behind a descriptor
such as `@classmethod`, `@staticmethod`, `functools.partialmethod`,
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

1) **Install websocket support** (unchanged):

```python
import falcon.asgi
from falcon_pachinko import websocket

app = falcon.asgi.App()
conn_mgr = websocket.install(app)
```

1) **Replace `add_websocket_route` with a router:**

```python
from falcon_pachinko import WebSocketRouter

router = WebSocketRouter()
router.add_route("/chat/{room}", ChatResource, history_size=100)
router.mount("/ws")
router.attach(app)
```

- Route paths are now **relative to the router mount point**.
- Attach only after mounting. `router.attach(app)` registers the Falcon
  responder that owns acceptance, text-frame receive and dispatch, and
  disconnect finalization. Direct `router.on_websocket()` calls are setup-only
  and do not run a receive loop.
- Resource initializer arguments moved from `args=`/`kwargs=` parameters to
  `*init_args`/`**init_kwargs` positional and keyword capture. Before:
  `router.add_route(path, Resource, args=(1,), kwargs={"n": 2})`; after:
  `router.add_route(path, Resource, 1, n=2)`. The `name` keyword is reserved
  for the route name and is never forwarded to the resource initializer.

1) **Move connection setup after acceptance:** `on_connect(req, ws, **params)`
   runs before the handshake and should validate the request without WebSocket
   I/O. Move connection registration and initial sends into `on_accepted(ws)`,
   which runs immediately after `ws.accept()`.

2) **Adopt resource composition and state sharing:**

- Move nested paths into `add_subroute` calls inside the parent resource.
- Use `self.state` (provided automatically) to share connection-scoped data.
- Override `get_child_context()` to inject custom state stores when needed.

1) **Switch to schema-driven dispatch:**

- Define a `msgspec` union `schema` on the resource.
- Register handlers with `@handles_message("tag")` or `on_tag` methods.
- Replace legacy `on_message` fallbacks with `on_unhandled`.

1) **Wire dependency injection via `resource_factory`:**

```python
from falcon_pachinko import ServiceContainer, WebSocketRouter

container = ServiceContainer()
container.register("conn_mgr", conn_mgr)
router = WebSocketRouter(resource_factory=container.create_resource)
```

Keep the manager returned by `install(app)` and pass it through dependency
injection. Slotted `falcon.asgi.App` instances do not expose
`app.ws_connection_manager`. A custom backend can be supplied by constructing
`WebSocketConnectionManager(backend=...)` and injecting that manager directly.

- Tests can supply alternate factories (see
  `falcon_pachinko.unittests.resource_factories.resource_factory`).

1) **Update connection manager usage:**

- Instantiate `WebSocketConnectionManager` with a backend when needed:

```python
from falcon_pachinko import ServiceContainer
from falcon_pachinko.websocket import WebSocketConnectionManager, MyBackend

container = ServiceContainer()
conn_mgr = WebSocketConnectionManager(backend=MyBackend(...))
container.register("conn_mgr", conn_mgr)
```

- `broadcast_to_room`, `connections`, and `send_to_connection` are now `async`
  and propagate errors directly.

1) **Move background tasks to lifespan workers:** replace
   `add_websocket_worker` with `WorkerController` and Falcon middleware whose
   `process_startup(scope, event)` starts workers and whose
   `process_shutdown(scope, event)` stops them. Add it with
   `app.add_middleware(...)`; do not use `@app.lifespan` for Falcon's ASGI
   application.

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
- **Behavioural tests:** exercise router mounting, nested paths, DI, and worker
  lifecycles with `pytest-bdd`.
- **Reference fixtures:** reuse `WebSocketTestClient` and `WebSocketSimulator`
  to validate both real and simulated websocket flows.

## Common Pitfalls

- Forgetting to call `router.mount(prefix)` or `router.attach(app)` leaves the
  router unavailable. Mount first, then attach it to the Falcon app.
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
