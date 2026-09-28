# ADR 0002: Split admission from post-accept opening

## Status

Proposed

## Context

The current `on_connect()` callback runs before `WebSocket.accept()`, although
its documentation describes a callback after the handshake. A boolean result
controls acceptance.

Some protocols can make an admission decision from the HTTP upgrade request.
Others intentionally accept first and then require an initial WebSocket
message, such as a bearer-bearing `client.hello`, before the normal dispatch
loop begins.

One callback cannot represent both phases without ambiguous send and receive
rules.

## Decision

Introduce two callbacks:

- `on_admit(req, session, **params) -> AdmissionDecision` runs before
  acceptance;
- `on_open(req, session, **params) -> None` runs after acceptance and before the
  automatic reader starts.

`AdmissionDecision` contains the accept or reject result, selected subprotocol,
and optional handshake response headers.

During `on_open()`, the resource has exclusive access to `session.receive()`.
This permits bounded first-message authentication or negotiation. After
`on_open()` returns, the active reader owns inbound receives and direct resource
receives raise `SessionPhaseError`.

The existing boolean-returning `on_connect()` remains a deprecated admission
callback for one documented migration window. The adapter invokes either
`on_admit()` or the legacy `on_connect()`, never both.

Add balanced hook pairs for admission and opening. The existing connect hook
names become deprecated aliases for admission hooks.

## Consequences

- Pre-accept rejection remains cheap and uses Falcon's normal handshake denial.
- Accepted-first authentication has an explicit, race-free home.
- Resources cannot accidentally race the active reader for inbound frames.
- Existing code keeps its current pre-accept behaviour during migration.
- The documentation must stop describing legacy `on_connect()` as
  post-handshake.
- Applications that send a welcome frame during legacy `on_connect()` must move
  that work to `on_open()` because Falcon forbids sending before acceptance.

## Alternatives considered

### Change `on_connect()` to run after acceptance

This silently reverses an existing runtime contract and removes the ability to
reject before the handshake.

### Authenticate only through HTTP headers

Header authentication is useful but cannot cover protocols that negotiate or
authenticate in their first WebSocket message.

### Start the reader before `on_open()`

This creates a race between opening logic and ordinary dispatch for the first
message.
