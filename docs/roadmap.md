# Falcon-Pachinko: Updated Development Roadmap

This roadmap outlines the implementation tasks for the Falcon-Pachinko
extension based on the revised, composable architecture detailed in the main
design document. It supersedes the previous roadmap and reflects a pivot
towards a more robust, scalable, and Falcon-idiomatic system.

Phases 1 to 5 record completed components, not a complete production session.
The remaining runtime work is specified in
[RFC 0001](rfcs/0001-websocket-session-runtime.md). Its ADRs supersede earlier
claims that a mounted router already drives message receipt or that a
connection backend can move live sockets between processes.

## 1. Foundational Composable Router

This phase replaces the initial `app.add_websocket_route` mechanism with the
more powerful and modular `WebSocketRouter`. This is the most significant
architectural change and underpins all subsequent features.

### 1.1. Composable router tasks

- [x] **Deprecate the old routing API.**

  - [x] Mark `app.add_websocket_route` and `app.create_websocket_resource` for
    deprecation. The logic will be entirely superseded by the new router.

- [x] **Implement** `WebSocketRouter` **as a Falcon Resource.**

  - [x] Create the new module `falcon_pachinko/router.py`.

  - [x] Define the `WebSocketRouter` class, ensuring it has an
    `on_websocket(req, ws)` responder method to make it a valid, mountable
    Falcon resource.

  - [x] Implement the router's internal path-matching logic, leveraging
    `falcon.routing.compile_uri_template` to handle routes relative to its
    mount point.

- [x] **Implement the** `WebSocketRouter.add_route()` **API.**

  - [x] The method must accept a relative path, a name for URL reversal, and the
    target resource.

  - [x] Add support for both `WebSocketResource` classes and callable factories
    as route targets, mirroring Falcon's HTTP routing flexibility.

  - [x] Add support for passing `*args` and `**kwargs` during route definition
    for resource initialization.

  - [x] Implement `router.url_for(name, **params)` for reverse URL generation.

- [x] **Update Core Tests.**

  - [x] Write new integration tests to verify that a `WebSocketRouter` can be
    mounted on a Falcon `App`.

  - [x] Test that connections to routes defined on the router correctly
    instantiate the associated resource with the correct path parameters and
    initialization arguments.

## 2. Advanced Dispatch and Resource Model

This phase refines the `WebSocketResource` to support the new schema-driven and
composable patterns.

### 2.1. Dispatch and resource model tasks

- [ ] **Integrate** `msgspec` **for Schema-Driven Dispatch.**

  - [x] Refactor the `WebSocketResource` dispatch loop to prioritize the
    `schema` attribute (a `msgspec` tagged union).

  - [x] Implement the logic to decode messages against the schema and route to
    handlers based on the message tag.

  - [x] Make the `@handles_message("type")` decorator the canonical, preferred
    way to register a handler.

  - [x] Implement the `on_{tag}` naming convention as a best-effort convenience,
    including the `CamelCase` to `snake_case` conversion.

  - [x] Document `msgspec`'s default strictness (no extra fields) and expose a
    `strict=False` option on the decorator.

- [ ] **Refine Resource API and State Management.**

  - [x] Rename the fallback handler method from `on_message` to `on_unhandled`
    to avoid ambiguity.

  - [x] Implement the `self.state` attribute on `WebSocketResource` as a
    swappable, dict-like proxy to facilitate external session stores. Provide
    guidance on this pattern for high-concurrency scenarios.

- [x] **Implement Nested Resource Composition.**

  - [x] Add the `add_subroute(path, resource, ...)` method to
    `WebSocketResource`.

  - [x] Enhance the `WebSocketRouter`'s matching logic to handle multi-level
    nested paths.

  - [x] Design and implement a robust context-passing mechanism for parent
    resources to inject state into child resources (see §5.2.3¹).

    - [x] Add an overridable `get_child_context()` hook on
      `WebSocketResource`¹ so parents can explicitly share data with the next
      child in the chain.

    - [x] Propagate a shared, connection-scoped `state` proxy unless a parent
      provides an alternative via `get_child_context()`¹.

    - [x] Update `WebSocketRouter` to instantiate resources sequentially,
      merging path params with parent-supplied context and passing along the
      shared `state`¹.

    - [x] Enhance `add_subroute()` to record child factories and static
      arguments while retaining a reference to the parent for router
      composition¹.

    - [x] Provide documentation and tests, such as injecting a `project`
      object into `TasksResource` and verifying modifications to shared
      `state`¹.

[¹](falcon-websocket-extension-design.md#523-context-passing-for-nested-resources)

## 3. Lifespan Workers and Connection Management

This phase implements the redesigned, ASGI-native background worker system and
finalizes the connection manager API.

### 3.1. Worker and connection management tasks

- [x] **Implement Lifespan-Based Worker Management.**

  - [x] Create the new `falcon_pachinko/workers.py` module.

  - [x] Implement the `WorkerController` class with its `start()` and `stop()`
    methods.

  - [x] Implement the optional `@worker` decorator for clarity.

  - [x] Update all examples and documentation to use the `@app.lifespan` pattern
    for managing workers, completely removing the old `add_websocket_worker`
    concept.

- [x] **Finalize** `WebSocketConnectionManager` **API.**

  - [x] Refactor all I/O methods (e.g., `broadcast_to_room`) to be `async def`
    and ensure they propagate exceptions correctly.

  - [x] Implement `async for` iterators (e.g., `conn_mgr.connections(room=...)`)
    for composable bulk operations.

  - [x] Define the abstract backend interface (ABC) for the connection manager
    to pave the way for future distributed backends.

  - [x] Ensure the default `InProcessBackend` correctly implements this new,
    robust interface.

## 4. Cross-Cutting Concerns

This phase adds the essential features for building production-grade
applications.

### 4.1. Cross-cutting concern tasks

- [x] **Implement the Multi-Tiered Hook System.**

  - [x] Create a `HookManager` to orchestrate hook execution.

  - [x] Add support for global hooks on `WebSocketRouter` and per-resource hooks
    on `WebSocketResource`.

  - [x] Implement the "onion-style" execution order (outermost hooks run first)
    and define the error propagation behaviour for exceptions raised within the
    hook chain.

- [x] **Design and Implement Dependency Injection.**

  - [x] Formalize a strategy for injecting shared services into ephemeral
    `WebSocketResource` instances, centring on a router-level resource factory
    that delegates instantiation to application-provided containers.

    - [x] Extend `WebSocketRouter.__init__` to accept an optional
      `resource_factory` callable, defaulting to the existing behaviour when
      not supplied.

    - [x] Update the router's resource instantiation flow to invoke the provided
      factory and ensure compatibility with nested resource composition.

    - [x] Add regression tests covering default instantiation, custom factories,
      and dependency injection into nested resources.

  - [x] Document usage patterns (including test-oriented factories) and update
    examples to demonstrate DI wiring.

## 5. Testing, Documentation, and Examples

This is an ongoing process, but it will be finalized in this phase to ensure
the library is ready for use.

### 5.1. Testing, documentation, and example tasks

- [x] **Develop Comprehensive Testing Utilities.**

  - [x] Implement a `WebSocketTestClient` built on the third-party
    `websockets` asyncio client, including context-managed connections and JSON
    helpers.

  - [x] Add trace/log capture to `WebSocketTestClient` sessions so tests can
    assert on frame ordering and payloads.

  - [x] Implement an injectable `WebSocketSimulator` that mimics the
    `WebSocketLike` interface with spyable send/receive queues.

  - [x] Extend `WebSocketRouter` with an optional `simulator_factory` hook used
    to supply `WebSocketSimulator` instances under test.

  - [x] Provide a pytest fixture that wires a simulator-backed router into the
    ASGI app, handles connection lifecycle management, and exposes helpers for
    pushing and inspecting messages.

- [x] **Build a Full Reference Example.**

  - [x] Create the `examples/reference_app` project showcasing router mounting,
    schema-driven dispatch, nested resources, hooks, dependency injection, and
    lifespan-managed workers end-to-end, backed by pytest unit tests and
    pytest-bdd behavioural coverage.

- [x] **Rewrite the Documentation.**

  - [x] Update the project's official documentation to reflect the new,
    composable architecture as the primary and recommended approach.

  - [x] Create a migration guide for users of the pre-release version.

  - [x] Add detailed "how-to" guides for advanced features like DI, state
    management, and custom connection manager backends.

## 6. Complete WebSocket Session Runtime

This phase turns the implemented router and dispatcher into one long-lived
production session, as specified by
[RFC 0001](rfcs/0001-websocket-session-runtime.md).

### 6.1. Session lifecycle tasks

- [ ] **Make the router own each complete session.**

  - [ ] Introduce `WebSocketSessionRunner` and make
    `WebSocketRouter.on_websocket()` await it until closure, following
    [ADR 0001](adr/0001-router-owns-session-lifecycle.md).

  - [ ] Implement explicit resolving, admission, opening, active, closing, and
    closed states with phase-checked operations.

  - [ ] Use `asyncio.TaskGroup` to supervise the reader, writer, and
    connection-scoped application tasks.

  - [ ] Preserve the first meaningful close code and invoke disconnect cleanup
    exactly once for peer disconnect, explicit close, failure, cancellation,
    and application shutdown.

  - [ ] Define the final routed resource as the lifecycle target while parent
    resources participate through hooks and shared state.

- [ ] **Split admission from post-accept opening.**

  - [ ] Add `AdmissionDecision`, `on_admit()`, and `on_open()` according to
    [ADR 0002](adr/0002-split-admission-from-opening.md).

  - [ ] Allow `on_open()` exclusive, bounded access to receive an initial
    protocol message before the automatic reader starts.

  - [ ] Add admission and opening hook pairs, including balanced error
    propagation and onion ordering.

  - [ ] Provide a documented deprecation adapter for the current
    boolean-returning `on_connect()` and connect-hook names.

- [ ] **Connect inbound frames to typed dispatch.**

  - [ ] Separate the low-level Falcon transport protocol from the
    application-facing session façade.

  - [ ] Introduce route- or resource-selected ingress codecs following
    [ADR 0003](adr/0003-use-explicit-ingress-codecs.md).

  - [ ] Make the default JSON text codec call `receive_text()` and pass the raw
    string to `WebSocketResource.dispatch()`.

  - [ ] Support binary and deliberately media-decoded protocols through
    explicit codecs without consuming a frame twice.

  - [ ] Treat normal `WebSocketDisconnected` events as session termination and
    map unexpected failures to deterministic close behaviour.

## 7. Flow Control and Connection-Scoped Work

This phase establishes enforceable output ordering, bounded buffering, and
supervised server-originated work.

### 7.1. Backpressure and supervision tasks

- [ ] **Serialize all outbound transport writes.**

  - [ ] Add bounded application-data and reserved control queues following
    [ADR 0004](adr/0004-serialize-writes-through-bounded-queues.md).

  - [ ] Make a dedicated writer task the sole caller of transport send methods.

  - [ ] Give each send command a completion future so callers observe send,
    timeout, disconnect, and serialization failures.

  - [ ] Add configurable finite queue bounds, enqueue timeouts, completion
    timeouts, and `SessionBackpressureError`.

  - [ ] Add metrics for queue depth, queue saturation, send latency, timeout,
    and close reason without logging payload values.

- [ ] **Supervise application event pumps.**

  - [ ] Add a session task-registration API that rejects new work after closing
    begins and assigns descriptive task names.

  - [ ] Cancel and await registered tasks when the session ends.

  - [ ] Propagate unhandled task failures to the runner and close unexpected
    failures with code `1011`.

  - [ ] Document application-owned acknowledgement, replay, coalescing, and
    compaction strategies as policies above the generic queue.

- [ ] **Restrict connection management to local sessions.**

  - [ ] Store session façades rather than raw Falcon WebSocket transports.

  - [ ] Route `send_to_connection()` and broadcasts through each session's
    writer and backpressure policy.

  - [ ] Rename and document backend contracts as process-local according to
    [ADR 0005](adr/0005-keep-connection-registries-process-local.md).

  - [ ] Remove claims that Redis or another backend can store or drive live
    connections across processes.

  - [ ] Document the separate event-bus-to-local-manager pattern for
    multi-process applications.

## 8. Security-Safe Diagnostics and Observability

This phase prevents the completed receive path from turning authentication and
protocol payloads into log confetti.

### 8.1. Security and diagnostic tasks

- [ ] **Resolve sensitive-payload exposure tracked by issue #61.**

  - [ ] Omit payload values from framework validation errors, logs, traces, and
    object representations by default, following
    [ADR 0007](adr/0007-omit-payload-values-from-diagnostics.md).

  - [ ] Exclude raw receive-hook fields from default `repr()` output.

  - [ ] Add an explicit recursive diagnostic sanitizer with configurable
    secret-key matching and strict depth, size, and collection limits.

  - [ ] Fail closed by omitting values that cannot be sanitized confidently.

  - [ ] Add canary-secret tests for nested mappings, lists, mixed key casing,
    malformed authentication frames, and short tokens near the start of a
    payload.

- [ ] **Add session-level operational telemetry.**

  - [ ] Record lifecycle phase, duration, close initiator, close code, failure
    class, task failure, and cleanup timeout without payload contents.

  - [ ] Expose stable structured metadata rather than requiring consumers to
    parse exception messages.

  - [ ] Document raw payload access in application hooks as a sensitive,
    explicitly trusted boundary.

## 9. Runtime-Parity Testing

This phase makes behavioural tests prove what the mounted production path
actually does.

### 9.1. Session contract tasks

- [ ] **Drive simulators through the real session runner.**

  - [ ] Refactor `SimulatorRouterHarness.connect()` according to
    [ADR 0006](adr/0006-share-the-session-runner-with-tests.md).

  - [ ] Run the router in a supervised background task while a simulated
    connection remains open.

  - [ ] Replace sleep-based coordination with observable lifecycle events and
    bounded waits.

  - [ ] Close the simulated client on fixture exit and await complete
    disconnect cleanup.

- [ ] **Add simulator and real-ASGI contract suites.**

  - [ ] Parameterize equivalent lifecycle scenarios over the simulator and
    `WebSocketTestClient`.

  - [ ] Verify route acceptance, opening-phase authentication, decorated
    handler dispatch, invalid messages, and binary codecs.

  - [ ] Verify peer and server close codes, exact-once cleanup, shutdown
    cancellation, and nested-resource hook ordering.

  - [ ] Verify concurrent producers never overlap transport writes.

  - [ ] Verify queue saturation, send timeout, application-task failure, and
    pending-future failure.

  - [ ] Verify payload canaries never appear in errors, logs, traces, or
    hook-context representations.

- [ ] **Exercise the reference application as a live session.**

  - [ ] Replace manual dispatch in examples and behavioural tests with messages
    sent through a mounted router.

  - [ ] Add a server-originated event-pump example using the supervised session
    task API.

  - [ ] Demonstrate a separate process-external event source feeding the local
    connection manager without presenting it as a distributed socket backend.

## 10. Migration and Beta Readiness

This phase aligns the public surface and documentation with the completed
runtime and prevents an incomplete API from reaching beta.

### 10.1. Release-readiness tasks

- [ ] **Update public documentation and examples.**

  - [ ] Reconcile `falcon-websocket-extension-design.md` with RFC 0001 and the
    accepted ADRs.

  - [ ] Update the user guide with admission, opening, codecs, task supervision,
    flow control, local registry scope, and close semantics.

  - [ ] Update the migration guide for lifecycle names, send completion
    semantics, simulator behaviour, and connection-backend restrictions.

  - [ ] Audit every example and code snippet to ensure it runs through the
    mounted session runtime.

- [ ] **Remove or delegate incomplete legacy paths.**

  - [ ] Make the deprecated `install()` routing helpers delegate to
    `WebSocketRouter` and `WebSocketSessionRunner`, or remove them before beta.

  - [ ] Ensure no public path accepts a connection and returns without driving
    its session.

  - [ ] Define and test the deprecation window for `on_connect()` and legacy
    connect-hook names.

- [ ] **Gate the first beta release.**

  - [ ] Require all RFC 0001 acceptance criteria to pass.

  - [ ] Require unit, behavioural, simulator, real-ASGI, security, type,
    formatting, spelling, Markdown, and Mermaid checks.

  - [ ] Publish explicit single-process and multi-process deployment guidance.

  - [ ] Remove alpha documentation claims that exceed the implemented
    lifecycle and delivery guarantees.
