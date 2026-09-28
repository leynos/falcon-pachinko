# ADR 0004: Serialize writes through bounded session queues

## Status

Proposed

## Context

Resource handlers, background event pumps, and
`WebSocketConnectionManager.broadcast_to_room()` may all send concurrently.
The current connection manager calls each underlying WebSocket directly. It has
a per-send timeout but no per-connection ordering, queue bound, or sole-writer
guarantee.

Concurrent transport writes make ordering and failure behaviour dependent on
the ASGI server. Unbounded buffering allows a slow client to retain memory
indefinitely.

## Decision

Each session will own a bounded application-data queue and a dedicated writer
task. The writer is the only task allowed to call transport send methods during
an active session.

`session.send_media()` enqueues a command carrying a completion future and
awaits that future. Callers therefore observe serialization failures and
disconnects rather than receiving false success after enqueueing.

Framework close and control commands use reserved capacity so a full data queue
cannot prevent orderly termination. Queue size, enqueue timeout, and send
completion timeout are configurable and finite.

Queue saturation raises `SessionBackpressureError`. The default generic policy
may close a persistently stalled session with close code `1013`.
Application-specific dropping, replacement, coalescing, acknowledgement, and
event compaction remain outside Falcon-Pachinko.

The connection manager stores session façades and sends through this API. It
never receives the raw transport.

## Consequences

- Transport writes are deterministic and serialized.
- Broadcasts may still run concurrently across different sessions.
- Per-session memory use has a calculable upper bound.
- Callers receive transport and timeout failures.
- A slow client cannot block unrelated sessions.
- Existing code that assumes `send_media()` writes immediately must rely on the
  documented completion semantics rather than direct transport access.
- Queue sizing and timeout defaults become part of the public operational
  contract and require metrics and tests.

## Alternatives considered

### Protect direct writes with a lock

A lock serializes writes but does not bound waiting producers or reserve
capacity for close commands.

### Use an unbounded queue

This avoids producer blocking by converting backpressure into memory growth.

### Put compaction policy in the framework

Compaction requires domain knowledge about which messages are replaceable and
which must remain durable.
