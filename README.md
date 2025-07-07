# falcon-pachinko

`falcon-pachinko` is an extension library for the
[Falcon](https://falcon.readthedocs.io) web framework. It adds a structured
approach to asynchronous WebSocket routing and background worker integration.

See
[docs/falcon-websocket-extension-design.md](docs/falcon-websocket-extension-design.md)
for the full design rationale.

## Key features

- `app.add_websocket_route()` maps WebSocket paths to `WebSocketResource`
  classes, mirroring Falcon's HTTP routing. Initialization parameters can be
  supplied, so one resource class supports multiple configurations.
- `WebSocketResource` provides `on_connect`, `on_disconnect`, and `on_message`
  lifecycle hooks.
- Message payloads are parsed into `msgspec.Struct` classes for speed and type
  safety.
- Define a `schema` union of tagged `msgspec.Struct` types to enable automatic
  dispatch based on the message tag.
- Use the canonical `@handles_message("type")` decorator to register message
  handlers.
- `WebSocketConnectionManager` tracks connections, manages rooms, and lets
  workers broadcast messages.
- Background tasks register via `app.add_websocket_worker` and interact with the
  connection manager.

These concepts are summarised in the design document:

```python
# pass route-specific options to the resource
app.add_websocket_route('/ws/chat/{room_name}', ChatRoomResource, history_size=100)
```

```python
@handles_message("new_chat_message")
async def handle_new_chat_message(self, ws, payload):
    ...
```

## Roadmap

Implementation tasks are tracked in [docs/roadmap.md](docs/roadmap.md). See the
[Release Workflow documentation](docs/release-workflow.md) for release details.

## Examples

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
