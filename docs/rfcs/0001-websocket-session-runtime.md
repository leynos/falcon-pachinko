# RFC 0001: Complete WebSocket session runtime

- Status: Proposed
- Date: 2026-08-25
- Owners: Falcon-Pachinko maintainers
- Target release: Before the first beta release

## Summary

Falcon-Pachinko already provides route composition, per-connection resources,
schema-driven message dispatch, hooks, dependency injection, connection
grouping, workers, and testing helpers. These components do not yet form a
complete production WebSocket session. The mounted router resolves a resource,
runs its connection callback, accepts the WebSocket, and returns without
receiving messages, dispatching them, supervising server-side work, or invoking
disconnect cleanup.

This RFC completes the missing runtime. The router will own each connection
until it closes and delegate the work to a `WebSocketSessionRunner`. The runner
will separate pre-accept admission from post-accept opening, connect inbound
frames to `WebSocketResource.dispatch()`, serialize outbound writes through a
bounded queue, supervise per-session tasks with structured concurrency, and
invoke disconnect hooks and cleanup exactly once.

The RFC also narrows `WebSocketConnectionManager` to process-local session
management. Applications that need cross-process delivery will connect their
own event bus to the local session manager rather than attempting to store live
WebSocket objects in a distributed backend.

The substantive decisions are recorded separately:

1. [ADR 0001: Let the router own the complete session lifecycle][adr-0001]
2. [ADR 0002: Split admission from post-accept opening][adr-0002]
3. [ADR 0003: Receive messages through an explicit ingress codec][adr-0003]
4. [ADR 0004: Serialize writes through bounded session queues][adr-0004]
5. [ADR 0005: Keep connection registries process-local][adr-0005]
6. [ADR 0006: Exercise one session runner in production and tests][adr-0006]
7. [ADR 0007: Exclude payload contents from diagnostics by default][adr-0007]

## Motivation

The current implementation contains the right building blocks but leaves
several critical gaps between them:

- `WebSocketRouter.on_websocket()` returns immediately after acceptance.
- Nothing in the production router receives client messages and calls
  `WebSocketResource.dispatch()`.
- `WebSocketResource.on_disconnect()` and disconnect hooks do not participate
  in the mounted router lifecycle.
- `on_connect()` runs before `accept()`, while its documentation describes a
  callback after the handshake.
- The transport protocol exposes `receive_media()`, which returns a
  deserialized object, while the dispatcher expects raw text or bytes.
- The connection manager can issue concurrent writes to the same underlying
  WebSocket and describes distributed backends that cannot safely hold live
  process-local socket objects.
- The simulator harness can verify routing and dispatch separately without
  proving that the production ASGI path connects them.

These gaps make a simple routed application appear complete in unit tests while
its deployed WebSocket responder has no active session loop. They also make
first-message authentication, server-originated event pumps, deterministic
cleanup, and backpressure difficult to implement without each application
building a private runtime around Falcon-Pachinko.

## Goals

The completed runtime must:

1. Keep `on_websocket()` alive until the client disconnects or the server closes
   the session.
2. Provide distinct pre-accept and post-accept lifecycle phases.
3. Support protocols that authenticate or negotiate using the first accepted
   message.
4. Feed inbound text or binary payloads into the existing typed dispatcher.
5. Guarantee that exactly one task writes to the underlying Falcon WebSocket.
6. Bound queued outbound work and expose explicit backpressure failure.
7. Supervise resource-owned event pumps and other per-session tasks.
8. Invoke disconnect hooks and `on_disconnect()` exactly once, including during
   cancellation and failure.
9. Keep all live connection handles process-local.
10. Use the same session runner with real Falcon transports and test
    simulators.
11. Prevent framework diagnostics from disclosing bearer tokens, passwords,
    API keys, or other payload values.
12. Preserve application ownership of domain-specific replay, acknowledgement,
    compaction, authorization, and event-bus policies.

## Non-goals

This RFC does not define:

- an application authentication protocol;
- a durable event log or replay store;
- acknowledgement or sequence-number semantics;
- application-specific event compaction;
- a Redis, Valkey, RabbitMQ, or Kafka client;
- automatic client reconnection;
- a multiplexed subscription protocol;
- HTTP WebSocket denial responses beyond Falcon's normal pre-accept close;
- domain-specific close codes in the `4000` to `4999` range.

Applications may build those behaviours on the generic session runtime.

## Terminology

- **Transport:** The Falcon WebSocket object, or a simulator implementing the
  same low-level operations. Only the session runner may receive from or write
  to it during an active session.

- **Session:** The connection-scoped façade passed to resources and stored in
  the local connection manager. It exposes safe send, close, opening-phase
  receive, and task-supervision operations without permitting unsynchronized
  transport I/O.

- **Admission:** The pre-accept phase in which the application inspects the HTTP
  request, headers, path parameters, and offered subprotocols.

- **Opening:** The post-accept phase before the automatic reader starts. The
  resource has exclusive access to receive an initial message for
  authentication or negotiation.

- **Active phase:** The phase in which the session reader owns inbound receives
  and routes messages to `dispatch()`.

- **Application task:** A connection-scoped coroutine, such as an event pump,
  registered with the session and supervised alongside the reader and writer.

## Proposed architecture

### Router and runner responsibilities

`WebSocketRouter` will remain responsible for route matching, nested resource
resolution, dependency injection, state propagation, and hook-manager
construction. Once it resolves a final target resource, it will create a
`WebSocketSessionRunner` and await it.

The runner owns:

- admission and handshake acceptance;
- opening-phase negotiation;
- the inbound reader;
- the outbound writer;
- application tasks;
- close initiation and close-code tracking;
- cancellation of sibling tasks;
- disconnect hooks and resource cleanup.

`WebSocketRouter.on_websocket()` must not detach the runner into an unobserved
task. Returning from the responder means the session has ended.

```text
Falcon App
    |
    v
WebSocketRouter
    |
    +-- resolve resource chain
    +-- construct HookManager
    |
    v
WebSocketSessionRunner
    |
    +-- admission
    +-- accept
    +-- opening
    +-- TaskGroup
    |     +-- reader
    |     +-- writer
    |     +-- application tasks
    |
    +-- disconnect cleanup
```

### Session phases

The runner will enforce the following state machine:

```text
RESOLVING
    |
    v
ADMISSION -- reject --> CLOSED
    |
    v
ACCEPTED
    |
    v
OPENING -- close/fail --> CLOSING
    |
    v
ACTIVE -- disconnect/fail/close --> CLOSING
    |
    v
CLOSED
```

Invalid phase operations raise a dedicated `SessionPhaseError`. Examples
include accepting twice, receiving directly after the active reader starts, or
starting a new application task after closing begins.

### Admission and opening API

The resource API will gain two explicit callbacks:

```python
class WebSocketResource:
    async def on_admit(
        self,
        req: falcon.Request,
        session: WebSocketSessionLike,
        **params: object,
    ) -> AdmissionDecision:
        return AdmissionDecision.accept()

    async def on_open(
        self,
        req: falcon.Request,
        session: WebSocketSessionLike,
        **params: object,
    ) -> None:
        return None
```

`AdmissionDecision` carries whether to accept, the selected subprotocol, and
optional handshake response headers. A rejection closes the unaccepted
transport, producing Falcon's normal handshake denial.

`on_open()` runs after acceptance and before the automatic reader starts. It
may call `session.receive()` to consume an initial protocol message. This
supports accepted-first protocols such as:

1. accept the WebSocket;
2. await a bounded `client.hello` message;
3. validate its bearer token;
4. send a welcome message or close with an application-defined code.

Once `on_open()` returns, the active reader becomes the sole owner of inbound
receives.

The existing boolean-returning `on_connect()` will remain as a deprecated
admission callback for one documented migration window. New code must use
`on_admit()` and `on_open()`. The migration adapter will never call both
`on_connect()` and `on_admit()` for the same resource.

### Inbound message path

The session runner will receive through an explicit ingress codec. The default
codec will accept JSON carried in text frames and return the raw string to
`WebSocketResource.dispatch()`. This preserves the current `msgspec` decode
path and avoids double deserialization.

A route or resource may select a binary codec that calls `receive_data()`, or a
custom codec that deliberately consumes `receive_media()`. The runner will not
probe by calling `receive_text()` and retrying with `receive_data()`, because a
wrong-type receive has already consumed the frame.

The active reader performs the following loop:

```python
while session.is_active:
    raw = await codec.receive(transport)
    await resource.dispatch(session, raw)
```

A normal client disconnect ends the loop without treating it as an application
failure. Decode failures continue to follow the resource's `on_unhandled()`
policy unless a future RFC defines a stricter protocol-error policy.

### Outbound message path

Resources, workers, connection-manager broadcasts, and application tasks will
send through the session façade. They must not write directly to the Falcon
transport.

`session.send_media()` will enqueue a send command and await its completion.
The command carries a completion future so callers still observe serialization
failures and disconnects. A dedicated writer task is the only task allowed to
call the transport's send methods.

Each session has:

- a bounded application-data queue;
- reserved capacity for close and framework-control commands;
- configurable enqueue and completion timeouts;
- deterministic failure of pending send futures when the session ends.

Queue saturation raises `SessionBackpressureError`. The default runtime may
close a persistently stalled session with standard close code `1013`, while
applications may provide a different policy. Domain-specific dropping,
coalescing, and compaction remain application concerns.

### Structured concurrency and application tasks

The runner will use `asyncio.TaskGroup`, which is available across the
project's supported Python versions. The reader, writer, and resource-owned
tasks share one failure domain.

The session façade will expose a structured task-registration operation, for
example:

```python
session.start_soon(resource.pump_events())
```

The final spelling and return type will be settled during implementation, but
the operation must:

- reject task creation once closing begins;
- cancel the task when the session ends;
- propagate unhandled task failures to the runner;
- give tasks descriptive names for diagnostics;
- prevent detached tasks from retaining the resource or transport.

An unexpected application-task failure closes the session with code `1011`
unless the resource converts it into an explicit close request.

### Disconnect and cleanup semantics

The final routed resource is the lifecycle target. Parent resources in a nested
chain continue to participate through onion-ordered hooks and shared state, but
the runner does not call every parent lifecycle method independently.

Cleanup follows this order:

1. stop accepting new send commands and application tasks;
2. cancel and await active session tasks;
3. fail unresolved send futures;
4. run `before_disconnect` hooks from outermost to innermost;
5. invoke the target resource's `on_disconnect()`;
6. run `after_disconnect` hooks from innermost to outermost;
7. close the transport if it is not already closed;
8. release references held by the runner.

The runner records the first meaningful close code and invokes cleanup exactly
once. Cancellation during application shutdown does not skip cleanup, although
a configurable cleanup timeout prevents a resource from blocking shutdown
indefinitely.

### Connection manager boundary

`WebSocketConnectionManager` will manage process-local session handles and room
membership. Its backend abstraction may vary the local data structure or
synchronization policy, but it must not claim to move live WebSocket objects
between processes.

The manager will store `WebSocketSessionLike` values rather than raw Falcon
transports. Therefore, all manager sends pass through the serialized writer.

Cross-process delivery uses a separate application event bus:

```text
producer or worker
        |
        v
durable store or broker
        |
        v
API process subscriber
        |
        v
local WebSocketConnectionManager
        |
        v
local session queues
```

Falcon-Pachinko may later define a generic notification-source adapter, but it
will remain distinct from the connection registry.

### Hook model

The hook model will gain explicit admission, opening, and balanced disconnect
events:

- `before_admit` and `after_admit`;
- `before_open` and `after_open`;
- `before_receive` and `after_receive`;
- `before_disconnect` and `after_disconnect`.

The existing `before_connect` and `after_connect` names will be deprecated
aliases for admission hooks during the same migration window as
`on_connect()`.

Hook contexts may expose raw payloads to explicitly registered application
hooks, but their representation and framework logging must not include payload
contents. Safe message metadata includes frame kind, byte length, handler tag
when known, and exception class.

### Error and close handling

The runtime will distinguish normal termination from failure:

- client disconnect: preserve the peer close code where Falcon exposes it;
- explicit application close: use the requested valid close code;
- unexpected internal error: close with `1011`;
- persistent generic backpressure: close with `1013`;
- pre-accept rejection: deny the handshake without inventing a WebSocket close
  code;
- application policy failure after acceptance: allow an application-defined
  code in the `4000` to `4999` range.

The runner must not replace an earlier, more informative close code with a later
cancellation artefact.

### Security and diagnostics

Framework-generated validation errors, log records, exception messages, hook
context representations, and trace summaries must omit payload values by
default. Truncation is not sanitization: a short bearer token can still fit
inside a truncated payload.

The security work tracked by issue #61 will:

- remove payload values from default validation errors;
- prevent raw frames from appearing in `repr(HookContext)`;
- recursively redact common secret-bearing keys in any explicit diagnostic
  formatter;
- enforce depth, size, and collection limits before formatting;
- test nested mappings, lists, mixed casing, malformed authentication frames,
  and token values at the start of a payload;
- document raw payload access as a sensitive application boundary.

### Test architecture

The simulator will implement only the transport boundary. It will not contain a
second, more capable lifecycle implementation.

`SimulatorRouterHarness.connect()` will start the real router and session runner
in a background task, wait until the session reaches `OPENING`, `ACTIVE`, or
`CLOSED`, and then yield a live connection helper. Pushing a message must wake
the same reader used in production. Exiting the harness closes the simulated
client and awaits the router task through disconnect cleanup.

A parameterized contract suite will run equivalent scenarios against:

- the simulator transport;
- a real Falcon ASGI application reached through `WebSocketTestClient`.

Required scenarios include:

- route resolution and acceptance;
- post-accept first-message authentication;
- text dispatch to a decorated handler;
- unhandled and invalid messages;
- binary codec dispatch;
- client and server close codes;
- exactly-once disconnect cleanup;
- cancellation during shutdown;
- concurrent producers with serialized transport writes;
- queue saturation and timeout;
- application-task failure;
- nested-resource hook ordering;
- payload-redaction invariants.

### Compatibility and migration

The project remains in alpha, so the implementation should prefer a coherent
runtime over preserving accidental semantics indefinitely. It should still
provide a bounded migration path:

1. introduce the new APIs and legacy adapters;
2. update the user guide, reference application, and examples;
3. emit deprecation warnings for legacy lifecycle names;
4. remove legacy names no earlier than the next minor pre-release after the
   first release containing the new runtime;
5. document any change to send completion semantics and connection-manager
   backend contracts.

The deprecated `install()` routing helpers remain outside the new runtime path.
They must either delegate to a mounted `WebSocketRouter` and session runner or
be removed before beta. They must not continue as a second incomplete routing
implementation.

## Implementation plan

### Milestone 1: Runtime skeleton

- Introduce transport, codec, session, runner, admission-decision, close-request,
  and runtime-error types.
- Add the lifecycle state machine and exact-once cleanup guard.
- Make the router await the runner.
- Connect the default text codec to `dispatch()`.

### Milestone 2: Lifecycle migration

- Add `on_admit()` and `on_open()`.
- Add new hook events and deprecated aliases.
- Implement opening-phase receive with phase enforcement.
- Update nested-resource lifecycle documentation.

### Milestone 3: Serialized output and supervision

- Add bounded data and control queues.
- Route all session and connection-manager sends through the writer.
- Add structured application-task registration.
- Define timeout, failure, and close-code behaviour.

### Milestone 4: Registry boundary and security

- Restrict connection backends to process-local session handles.
- Remove distributed-socket claims from documentation.
- Resolve issue #61 and add safe diagnostic metadata.
- Add migration guidance for application event buses.

### Milestone 5: Contract tests and release gate

- Refactor the simulator harness around the real runner.
- Add real ASGI contract tests.
- Update the reference application to exercise a live session.
- Require the contract suite, security tests, Markdown checks, and API
  documentation before beta.

## Alternatives considered

### Let every resource implement its own receive loop

This is Falcon's native low-level model, but it duplicates lifecycle,
supervision, backpressure, hook, and cleanup code in every application. It also
makes Falcon-Pachinko's dispatcher and testing abstractions largely ornamental.

### Start the session as a detached task and return from the router

Detached tasks hide failures from Falcon and the ASGI server, complicate
shutdown, and allow resources to outlive their request scope. Structured
ownership is safer and easier to test.

### Use `receive_media()` as the universal inbound API

This supports mixed frame types but deserializes before the `msgspec` dispatcher
and may apply arbitrary application media handlers. An explicit codec makes the
wire contract visible and preserves the current fast path.

### Store connection objects in Redis or another distributed backend

A live WebSocket belongs to one event loop in one process and cannot be
serialized or driven remotely. A broker can distribute events, not socket
objects.

### Permit direct transport writes and merely document the risk

Documentation cannot prevent concurrent writes from resource handlers,
broadcasts, and event pumps. A session façade and sole writer make the invariant
enforceable.

## Acceptance criteria

The RFC is implemented when all of the following hold:

- A real client can connect through a mounted router, send a valid message, and
  observe its registered handler run without manual dispatch.
- `on_open()` can receive and validate an initial message after acceptance.
- `on_disconnect()` and balanced disconnect hooks run exactly once for normal
  disconnect, explicit close, task failure, and application shutdown.
- No two tasks can call an underlying transport send method concurrently.
- Bounded queues and timeouts have deterministic, tested behaviour.
- Connection-manager backends expose only process-local session semantics.
- Simulator and real-ASGI contract suites exercise the same runner.
- Built-in diagnostics do not disclose payload values.
- The user guide, migration guide, reference application, design document, and
  roadmap describe the completed runtime accurately.
- The first beta release is blocked until these criteria pass.

[adr-0001]: ../adr/0001-router-owns-session-lifecycle.md
[adr-0002]: ../adr/0002-split-admission-from-opening.md
[adr-0003]: ../adr/0003-use-explicit-ingress-codecs.md
[adr-0004]: ../adr/0004-serialize-writes-through-bounded-queues.md
[adr-0005]: ../adr/0005-keep-connection-registries-process-local.md
[adr-0006]: ../adr/0006-share-the-session-runner-with-tests.md
[adr-0007]: ../adr/0007-omit-payload-values-from-diagnostics.md
