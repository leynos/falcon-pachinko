# Falcon-Pachinko Roadmap

This roadmap outlines the implementation tasks for the Falcon-Pachinko extension.
Line numbers refer to `docs/falcon-websocket-extension-design.md` after
formatting.

1. **Foundation and API Setup** (lines 140-170)

   - [x] Implement `falcon_pachinko.install(app)` to attach a
     `WebSocketConnectionManager` to the Falcon `App`.
   - [x] Provide `app.add_websocket_route()` mirroring Falcon’s HTTP routing.

2. **WebSocketResource Base Class** (lines 207-247)

   - [x] Create `WebSocketResource` with `on_connect`, `on_disconnect`, and
     `on_message` lifecycle methods.
   - [x] Include connection-specific state (one instance per connection).
   - [x] Add decorator `@handles_message("type")` for dispatching JSON messages
     by their `type`.

3. **Connection Manager** (lines 288-340)

   - [ ] Implement `WebSocketConnectionManager` with methods for managing
     connections and rooms: `add_connection`, `remove_connection`, `join_room`,
     `leave_room`, `broadcast_to_room`, `send_to_connection`,
     `get_connections_in_room`, and `get_rooms_by_prefix`.
   - [ ] Provide helper methods in `WebSocketResource` for common room
     operations.

4. **Background Worker Integration** (lines 342-375)

   - [ ] Expose `app.add_websocket_worker` to register asynchronous workers that
     use `app.ws_connection_manager`.
   - [ ] Ensure workers can be started with the application’s lifespan events.

5. **Testing Utilities**

   - [ ] Develop tools similar to `falcon.testing` that simulate WebSocket
     clients for unit and integration tests.
   - [ ] Enable sending and receiving messages and verifying connection events.

6. **Illustrative Example**

   - [ ] Implement the chat application example from the design to serve as a
     reference and initial API validation.

7. **Future Enhancements** (lines 703-758)

   - [ ] Explore advanced subprotocol handling and compression support.
   - [ ] Investigate enhanced connection grouping strategies.
   - [ ] Make the connection manager pluggable for distributed deployments.
   - [ ] Consider automatic AsyncAPI stub generation based on resources and
     handlers.
