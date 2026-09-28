# ADR 0005: Keep connection registries process-local

## Status

Proposed

## Context

`ConnectionBackend` stores mappings from connection identifiers to live
WebSocket objects. The design documentation describes a future distributed
backend, such as Redis Pub/Sub, that would allow broadcasting across server
processes.

A live WebSocket is owned by one ASGI application process and event loop. It
cannot be serialized into Redis or driven by another process. A distributed
backend can transport events or membership metadata, but not the connection
handle itself.

Conflating these responsibilities obscures delivery guarantees and encourages
unsafe backend implementations.

## Decision

`WebSocketConnectionManager` is a process-local registry of
`WebSocketSessionLike` handles and room membership.

Its backend abstraction may provide alternative in-process storage,
synchronization, indexing, or instrumentation. Every implementation must keep
session handles in the owning process.

Cross-process fan-out is an application integration between an external event
source and each process's local manager. The event source may be a broker,
database notification stream, or durable event log, but it is not a connection
backend.

Documentation and names will state this boundary explicitly. Any future
Falcon-Pachinko broker integration will use a separate interface.

## Consequences

- The manager can enforce the session writer and backpressure invariants.
- Multi-process applications need one event-bus subscriber per API process or
  an equivalent delivery topology.
- Durable replay and ordering remain properties of the application's event
  source.
- The existing claim that application code can swap in a distributed
  connection backend without architectural change must be removed.
- Backend tests can assume all session handles belong to the current process
  and event loop.

## Alternatives considered

### Store connection identifiers in Redis and look up sockets remotely

A remote process still cannot access the event-loop-bound socket associated
with the identifier.

### Hide broker fan-out behind `ConnectionBackend`

This joins event transport, membership, and live connection storage into one
interface with incompatible failure and consistency semantics.

### Remove the manager entirely

A local manager remains useful for room membership, server-originated sends,
testing, and process-local observability.
