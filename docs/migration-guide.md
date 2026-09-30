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
safe type name.

Close failures still propagate the original close exception. Their trace entry
now records the exception class name instead of its message; caller-supplied
close `code` and `reason` fields retain their values.

## Step-by-Step Migration

1) **Install websocket support** (unchanged):

```python
from falcon_pachinko import websocket

app = falcon.App()
websocket.install(app)
```

1) **Replace `add_websocket_route` with a router:**

```python
from falcon_pachinko import WebSocketRouter

router = WebSocketRouter()
router.add_route("/chat/{room}", ChatResource, history_size=100)
router.mount("/ws")
app.add_route("/ws", router)
```

- Route paths are now **relative to the router mount point**.
- Resource initializer arguments moved from `args=`/`kwargs=` parameters to
  `*init_args`/`**init_kwargs` positional and keyword capture. Before:
  `router.add_route(path, Resource, args=(1,), kwargs={"n": 2})`; after:
  `router.add_route(path, Resource, 1, n=2)`. The `name` keyword is reserved
  for the route name and is never forwarded to the resource initializer.

1) **Adopt resource composition and state sharing:**

- Move nested paths into `add_subroute` calls inside the parent resource.
- Use `self.state` (provided automatically) to share connection-scoped data.
- Override `get_child_context()` to inject custom state stores when needed.

1) **Switch to schema-driven dispatch:**

- Define a `msgspec` union `schema` on the resource.
- Register handlers with `@handles_message("tag")` or `on_tag` methods.
- Replace legacy `on_message` fallbacks with `on_unhandled`.

1) **Wire dependency injection via `resource_factory`:**

```python
from falcon_pachinko import ServiceContainer

container = ServiceContainer()
container.register("conn_mgr", app.ws_connection_manager)
router = WebSocketRouter(resource_factory=container.create_resource)
```

- Tests can supply alternate factories (see
  `falcon_pachinko.unittests.resource_factories.resource_factory`).

1) **Update connection manager usage:**

- Instantiate `WebSocketConnectionManager` with a backend when needed:

```python
from falcon_pachinko.websocket import WebSocketConnectionManager, MyBackend

app.ws_connection_manager = WebSocketConnectionManager(backend=MyBackend(...))
```

- `broadcast_to_room`, `connections`, and `send_to_connection` are now `async`
  and propagate errors directly.

1) **Move background tasks to lifespan workers:**

- Replace `add_websocket_worker` with `WorkerController` and `@app.lifespan`
  hooks to start/stop workers.

## Testing Checklist

- **Unit tests:** cover resource constructors, state handling, and any custom
  connection backend logic.
- **Behavioural tests:** exercise router mounting, nested paths, DI, and worker
  lifecycles with `pytest-bdd`.
- **Reference fixtures:** reuse `WebSocketTestClient` and `WebSocketSimulator`
  to validate both real and simulated websocket flows.

## Common Pitfalls

- Forgetting to call `router.mount(prefix)` results in 404s; mount before
  hooking into the Falcon app.
- Custom backends must implement *all* methods from `ConnectionBackend`,
  raising `ValueError` on duplicate IDs and ignoring stale room entries in
  `snapshot` or documenting alternative semantics.
- When migrating message handlers, ensure schema tags match incoming payloads;
  extra fields are rejected unless `strict=False` is used on `@handles_message`.
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
