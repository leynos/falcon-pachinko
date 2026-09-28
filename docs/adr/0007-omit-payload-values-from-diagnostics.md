# ADR 0007: Exclude payload contents from diagnostics by default

## Status

Proposed

## Context

Validation errors may currently include a truncated string representation of an
invalid payload. Truncation limits length but does not hide passwords, bearer
tokens, API keys, or other sensitive values near the beginning of the payload.

Receive hooks also carry raw frames. A dataclass representation, generic
exception formatter, or debug log can therefore disclose a complete
authentication message without an application explicitly choosing to log it.

Field-name redaction helps when producing an intentional diagnostic sample, but
it cannot reliably identify every secret-bearing field.

## Decision

Framework-generated errors, log records, trace summaries, and object
representations will omit payload values by default.

Validation errors may report structural information such as unknown field
names, expected type, frame kind, payload length, and exception class. They must
not include the raw frame or decoded values.

Raw payloads may remain accessible to explicitly registered receive hooks and
handlers, but raw fields must be excluded from default `repr()` output and
generic logging helpers.

An opt-in diagnostic formatter may produce a sanitized sample. It must:

- recursively redact configurable secret-bearing keys;
- match keys case-insensitively and normalize common separators;
- cover mappings nested inside lists and tuples;
- limit depth, collection length, string length, and total output;
- represent bytes without decoding arbitrary secret material;
- fail closed by omitting a value when sanitization is uncertain.

Security tests will use distinctive canary secrets and assert that they never
appear in error strings, logs, hook-context representations, or trace output.

## Consequences

- Default diagnostics become safe for centralized logging and error tracking.
- Developers lose the convenience of seeing complete invalid payloads in an
  exception and must opt into a sanitized formatter when necessary.
- Raw hook access remains a trusted application boundary and must be documented
  as sensitive.
- Security issue #61 becomes a release blocker for the session runtime.
- Diagnostic metadata needs a stable structure so observability does not depend
  on parsing exception prose.

## Alternatives considered

### Keep truncating payload strings

Short secrets and secrets near the start of a payload remain visible.

### Redact only a fixed list of keys by default

Unknown field names, positional values, and encoded secrets can bypass the
list. Omission is the safer default.

### Remove all raw-payload hook access

Some applications legitimately need explicit audit, protocol, or validation
hooks. The framework should make access deliberate rather than impossible.
