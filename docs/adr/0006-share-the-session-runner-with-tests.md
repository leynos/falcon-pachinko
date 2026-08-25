# ADR 0006: Exercise one session runner in production and tests

## Status

Proposed

## Context

The simulator harness currently awaits `WebSocketRouter.on_websocket()` before
yielding a connection helper. This works because the router returns immediately
after acceptance. Tests can then push a message and call dispatch through
separate helper paths.

Once the router owns a long-lived session, a harness that models its own
lifecycle would risk repeating the present defect: tests could validate a more
complete system than production uses.

## Decision

The simulator will implement only the low-level transport boundary. The real
`WebSocketSessionRunner` will drive it.

`SimulatorRouterHarness.connect()` will:

1. start `router.on_websocket()` in a supervised background task;
2. wait for an observable opening, active, or closed state;
3. yield a helper while the router task remains alive;
4. push inbound frames into the simulator transport queue;
5. close the simulated client on context exit;
6. await the router task through disconnect cleanup.

A contract suite will parameterize equivalent scenarios over the simulator and
a real Falcon ASGI application reached through `WebSocketTestClient`.

Unit tests may still test dispatch or queue components directly, but behavioural
claims about a mounted WebSocket route must pass through the session runner.

## Consequences

- Simulator tests exercise the same lifecycle, cancellation, dispatch, and
  cleanup code as production.
- The harness becomes asynchronous for the entire connection lifetime.
- Tests must close sessions explicitly or through context management.
- Race-prone sleep-based assertions should be replaced with state events or
  bounded waits.
- Real-ASGI tests detect integration differences in Falcon and the ASGI server
  that a simulator cannot model.

## Alternatives considered

### Give the simulator its own receive loop

A second runtime would duplicate semantics and could drift from production.

### Test only with a real ASGI server

Real-server tests are valuable but slower and less precise for fault injection,
queue saturation, and cancellation interleavings.

### Keep routing and dispatch tests separate

Separate unit tests remain useful, but they cannot prove that the mounted
production path connects the components.
