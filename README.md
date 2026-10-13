# falcon-pachinko

[![Ask DeepWiki](https://deepwiki.com/badge.svg)](
https://deepwiki.com/leynos/falcon-pachinko)

`falcon-pachinko` is an extension library for the
[Falcon](https://falcon.readthedocs.io) web framework. It adds a structured
approach to asynchronous WebSocket routing and background worker integration.

See
[docs/falcon-websocket-extension-design.md](docs/falcon-websocket-extension-design.md)
for the full design rationale. For practical guidance, start with the
[User Guide](docs/users-guide.md) and the
[Migration Guide](docs/migration-guide.md). Maintainer-facing notes live in the
[Developer Guide](docs/developers-guide.md),
including the
[nose code duplication gate](docs/developers-guide.md#nose-code-duplication-gate).

## Requirements

- Python 3.12 or newer
- Falcon 4.x with ASGI support

## Key features

- `WebSocketRouter` mounts nested WebSocket routes on Falcon with
  `router.mount(prefix)` followed by `router.attach(app)`.
- `WebSocketResource` provides `on_connect`, `on_accepted`, `on_disconnect`,
  and `on_unhandled` lifecycle hooks. The router owns the production receive
  loop and dispatches text frames.
- Message payloads are parsed into `msgspec.Struct` classes for speed and type
  safety.
- Define a `schema` union of tagged `msgspec.Struct` types to enable automatic
  dispatch based on the message tag. Every member must use the same kind of
  tag: all string tags, or all integer tags.
- Use the canonical `@handles_message("type")` decorator to register message
  handlers.
- `WebSocketConnectionManager` tracks connections, manages rooms, and lets
  workers broadcast messages.
- Background tasks run under Falcon's ASGI lifespan via `WorkerController`
  and middleware `process_startup` / `process_shutdown` methods.

These concepts are summarized in the design document:

```python
router.add_route("/chat/{room_name}", ChatRoomResource, history_size=100)
router.mount("/ws")
router.attach(app)
```

```python
@handles_message("new_chat_message")
async def handle_new_chat_message(self, ws, payload): ...
```

Use ``conn_mgr.connections()`` to iterate over active WebSocket connections.
The iterator captures a snapshot under a lock and then yields it, so iteration
occurs over an immutable snapshot.

Use Falcon middleware `process_startup(scope, event)` to call
`WorkerController.start()` and `process_shutdown(scope, event)` to call
`stop()`. See the [User Guide](docs/users-guide.md#9-background-workers) for a
complete example.

## Roadmap

Implementation tasks are tracked in [docs/roadmap.md](docs/roadmap.md). See the
[Release Workflow documentation](docs/release-workflow.md)
for release details.

## Examples

### Reference application

`examples/reference_app` is the live end-to-end WebSocket example. Start it
from the repository root with either command:

```bash
python -m examples.reference_app.server
# or
uvicorn --factory examples.reference_app.server:create_app --host 127.0.0.1 --port 8000
```

Connect to `ws://127.0.0.1:8000/ws/workspaces/atlas/projects/triage/tasks` with
the headers `x-workspace-token: seekrit` and `x-user: casey`. The nested
workspace, project and task route resolves through `WorkspaceResource`,
`ProjectResource`, and `TaskStreamResource`; the `before_connect` hook checks
the token. The app emits `session.ready` and accepts a `task.add` message. The
client receives `task.added`; the lifespan-managed announcement worker
broadcasts an `announcement` with kind `task_added`. The router accepts the
socket, calls `on_accepted`, and owns the text-frame receive and dispatch loop.
Disconnect hooks run before resource cleanup, which removes the socket from the
connection manager. The example exposes its dependency container as
`app.reference_services`, including the audit trail, workspace repository, and
connection manager.

The live Falcon ASGI journey is exercised by
`tests/behaviour/reference_example_live.feature`.

An end-to-end demonstration lives under `examples/random_status`. It shows how
to:

- expose an HTTP endpoint returning the current status
- handle a WebSocket message that updates that status in an SQLite database
  using `aiosqlite`
- send a periodic "random" message from a background task.

Run `server.py` with Uvicorn and connect using the provided `client.py` to
observe the interaction.

## License

This project is licensed under the terms of the ISC license. See
[LICENSE](LICENSE) for details.
