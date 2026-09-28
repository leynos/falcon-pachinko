# ADR 0001: Let the router own the complete session lifecycle

## Status

Proposed

## Context

`WebSocketRouter` currently resolves a resource, invokes its connection
callback, accepts or closes the WebSocket, and returns. The production path does
not receive client messages, invoke `dispatch()`, supervise server-originated
work, or call disconnect cleanup.

A long-lived WebSocket session needs one clear owner. Without one, resources,
workers, test harnesses, and applications each grow their own loops and
cancellation rules.

## Decision

`WebSocketRouter.on_websocket()` will await a `WebSocketSessionRunner` after
route and resource resolution. The responder will not return until the session
has closed.

The runner will own admission, acceptance, opening, inbound reads, outbound
writes, resource-owned tasks, close initiation, and exact-once disconnect
cleanup. It will use `asyncio.TaskGroup` to keep the reader, writer, and
application tasks in one failure domain.

The final routed resource is the lifecycle target. Parent resources in a nested
chain participate through hooks and shared state rather than receiving
duplicate lifecycle callbacks.

Normal peer disconnects end the task group without becoming application
failures. Unexpected reader, writer, handler, or application-task failures
cancel sibling tasks, request an appropriate close, run cleanup, and then
propagate to the responder.

## Consequences

- The mounted router becomes a complete WebSocket responder rather than a
  connection factory.
- Falcon and the ASGI server retain visibility of session failures.
- Application shutdown can cancel and await sessions deterministically.
- `on_disconnect()` and disconnect hooks can have exact-once semantics.
- Existing tests that expect `on_websocket()` to return immediately must become
  live-session tests.
- Long-running handler work must move into a supervised application task when
  it should run concurrently with inbound dispatch.

## Alternatives considered

### Resource-owned receive loops

This duplicates lifecycle and supervision code and bypasses the framework's
dispatcher in simple applications.

### Detached session tasks

A detached task hides errors from the responder and makes shutdown and resource
ownership ambiguous.

### A global session supervisor

A global registry can observe sessions, but it does not replace the local
ownership relationship between one responder and one connection.
