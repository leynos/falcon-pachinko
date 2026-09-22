# ADR 0003: Receive messages through an explicit ingress codec

## Status

Proposed

## Context

`WebSocketResource.dispatch()` expects raw `str` or `bytes` values and uses
`msgspec` to decode them. The current `WebSocketLike` protocol exposes
`receive_media()`, which asks Falcon's configured media handler to deserialize
the frame and returns an arbitrary object.

Falcon exposes separate public methods for receiving text and binary frames.
Calling one and retrying with the other is unsafe because the wrong-type call
has already consumed the frame.

The runtime therefore needs an explicit wire-format decision.

## Decision

The session runner will receive through an `IngressCodec` selected by the route
or resource.

The default codec is a JSON text codec. It calls the transport's
`receive_text()` method and returns the raw string to the existing dispatcher.
This preserves `msgspec` as the sole JSON decoder on the default path.

A binary codec may call `receive_data()` and return bytes. Applications that
deliberately use Falcon media handlers may provide a codec based on
`receive_media()` and a matching dispatcher adapter.

The runner will not inspect Falcon private ASGI receive attributes and will not
probe frame type by consuming a frame twice.

The selected codec also owns opening-phase `session.receive()` so initial
authentication and active dispatch use the same wire contract.

## Consequences

- The default protocol is explicit: JSON in WebSocket text frames.
- Schema dispatch retains its current raw `msgspec` decode path.
- Binary and custom-media protocols remain possible without hidden global
  configuration.
- Mixed text and binary application messages require a codec designed for that
  protocol.
- The low-level transport protocol must expose `receive_text()`,
  `receive_data()`, and, where needed, `receive_media()`.
- Tests can inject deterministic codecs and wrong-frame-type scenarios.

## Alternatives considered

### Always use `receive_media()`

This double-decodes the common JSON path, depends on arbitrary Falcon media
handlers, and changes what `on_unhandled()` receives.

### Try `receive_text()` and then `receive_data()`

The first call consumes the frame before reporting the payload-type mismatch.

### Reach into Falcon's private ASGI receiver

Private internals would tightly couple Falcon-Pachinko to Falcon implementation
details and bypass Falcon's disconnect handling.
