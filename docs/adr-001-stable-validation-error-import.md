# ADR 001: Stable ValidationError import

Status: Accepted

## Context

Payload decoding and unknown-field validation currently use
`msgspec.ValidationError`. Callers need a stable import path that keeps them
decoupled from the validation library used internally.

## Decision

Expose `ValidationError` from `falcon_pachinko.utils` as an identity alias of
`msgspec.ValidationError`, and raise that alias for unknown fields. Keep the
dispatcher fallback behaviour unchanged: validation failures from decoding are
routed to `on_unhandled`, while a `ValidationError` raised inside a handler
propagates. Do not add a package-root export.

## Consequences

Callers can import the exception from `falcon_pachinko.utils`, and existing
`except msgspec.ValidationError` handlers continue to catch it. The alias is a
public boundary; any future validation-library change must preserve that
compatibility contract.
