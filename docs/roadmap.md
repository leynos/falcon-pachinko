# Falcon-Pachinko: Updated Development Roadmap

This roadmap outlines the implementation tasks for the Falcon-Pachinko
extension based on the revised, composable architecture detailed in the main
design document. It supersedes the previous roadmap and reflects a pivot
towards a more robust, scalable, and Falcon-idiomatic system.

## Phase 1: Foundational Composable Router

This phase replaces the initial `app.add_websocket_route` mechanism with the
more powerful and modular `WebSocketRouter`. This is the most significant
architectural change and underpins all subsequent features.

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

## Phase 2: Advanced Dispatch and Resource Model

This phase refines the `WebSocketResource` to support the new schema-driven and
composable patterns.

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
    resources to inject state into child resources (see
    [§5.2.3](falcon-websocket-extension-design.md#523-context-passing-for- nested-resources)
     ).

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

## Phase 3: Lifespan Workers and Connection Management

This phase implements the redesigned, ASGI-native background worker system and
finalizes the connection manager API.

- [x] **Implement Lifespan-Based Worker Management.**

  - [x] Create the new `falcon_pachinko/workers.py` module.

  - [x] Implement the `WorkerController` class with its `start()` and `stop()`
    methods.

  - [x] Implement the optional `@worker` decorator for clarity.

  - [ ] Update all examples and documentation to use the `@app.lifespan` pattern
    for managing workers, completely removing the old `add_websocket_worker`
    concept.

- [ ] **Finalize** `WebSocketConnectionManager` **API.**

  - [ ] Refactor all I/O methods (e.g., `broadcast_to_room`) to be `async def`
    and ensure they propagate exceptions correctly.

  - [ ] Implement `async for` iterators (e.g., `conn_mgr.connections(room=...)`)
    for composable bulk operations.

  - [ ] Define the abstract backend interface (ABC) for the connection manager
    to pave the way for future distributed backends.

  - [ ] Ensure the default `InProcessBackend` correctly implements this new,
    robust interface.

## Phase 4: Cross-Cutting Concerns

This phase adds the essential features for building production-grade
applications.

- [ ] **Implement the Multi-Tiered Hook System.**

  - [ ] Create a `HookManager` to orchestrate hook execution.

  - [ ] Add support for global hooks on `WebSocketRouter` and per-resource hooks
    on `WebSocketResource`.

  - [ ] Implement the "onion-style" execution order (outermost hooks run first)
    and define the error propagation behaviour for exceptions raised within the
    hook chain.

- [ ] **Design and Implement Dependency Injection.**

  - [ ] Formalize and implement a strategy for injecting shared services into
    ephemeral `WebSocketResource` instances. This will likely involve allowing
    a DI container or factory to be provided to the `WebSocketRouter`.

## Phase 5: Testing, Documentation, and Examples

This is an ongoing process, but it will be finalized in this phase to ensure
the library is ready for use.

- [ ] **Develop Comprehensive Testing Utilities.**

  - [ ] Build a `WebSocketTestClient` or `WebSocketSimulator` for high-level
    integration testing.

  - [ ] Ensure the test client API is intuitive and supports the full connection
    and message lifecycle.

  - [ ] Provide pytest fixtures to simplify test setup.

- [ ] **Build a Full Reference Example.**

  - [ ] Create a new, comprehensive example application that demonstrates all
    the advanced features working in concert: a mountable router, schema-driven
    dispatch, nested resources, lifespan workers, hooks, and dependency
    injection.

- [ ] **Rewrite the Documentation.**

  - [ ] Update the project's official documentation to reflect the new,
    composable architecture as the primary and recommended approach.

  - [ ] Create a migration guide for users of the pre-release version.

  - [ ] Add detailed "how-to" guides for advanced features like DI, state
    management, and custom connection manager backends.
