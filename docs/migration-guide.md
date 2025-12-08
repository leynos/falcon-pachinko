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

## Done?

- Remove deprecated calls to
  `app.add_websocket_route`/`create_websocket_resource`.
- Update documentation links to point at `docs/users-guide.md`.
- Re-run `make check-fmt`, `make typecheck`, `make lint`, and `make test` to
  confirm the upgraded codebase passes all gates.
