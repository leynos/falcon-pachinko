# ADR 003: Schema-mode strict validation before conversion

Status: Accepted

## Context

Schema dispatch decoded a frame directly into the resource's tagged
`msgspec.Struct` union. `msgspec` discards fields the selected Struct does not
declare during that decode, so by the time the framework ran its strict
unknown-field check the evidence was gone. A strict handler could therefore run
on a message that violated its no-extra-fields contract. Envelope dispatch did
not share the defect because it validates the decoded `payload` mapping before
converting it.

## Decision

Decode schema frames generically and validate before conversion:

1. Decode the frame with `msgspec.json.decode` into a plain mapping, retaining
   every key.
2. Resolve the schema member from the tag field. Missing, wrong-kind, boolean
   and unknown tags fall back to `on_unhandled`.
3. Validate the retained mapping against the resolved member when the handler
   is strict. The allowed keys are the member's encoded wire names plus its tag
   field.
4. Decode the original frame into the resolved member with
   `msgspec.json.decode`. The generically decoded mapping is inspection-only:
   converting it would lose wire-level detail, turning integer mapping keys
   into strings, `msgspec.Raw` fields into decoded JSON, and `Decimal` fields
   into `float`-derived values. A `strict=False` handler that hits a wire
   decode rejection falls back to `msgspec.convert` of the retained mapping so
   lax coercion still applies.

Both dispatch modes now share one validate-then-convert path. `strict=False`
has the same meaning in both: undeclared top-level fields are ignored and
`msgspec` lax coercion is permitted. A Struct declared with
`forbid_unknown_fields=True` still rejects extras because that is the Struct's
own contract, not the framework's check.

The framework check is shallow. Nested Structs decide their own unknown-field
behaviour through `forbid_unknown_fields`, and `msgspec` enforces it during
conversion.

Schema members declared with `array_like=True` are rejected as a configuration
error: such a Struct has no keyed tag on the wire, so tag resolution could
never select it. Invalid schemas raise `TypeError`; only validation and
conversion `ValidationError`s become `on_unhandled` fallbacks.

### Rejected alternatives

- **Require or generate Struct variants with `forbid_unknown_fields=True`.**
  The library does not own application Structs, and generating variants would
  change the types handlers receive, duplicate schemas at class creation, and
  leave the same hole for a runtime-assigned schema. It would also make
  `strict=False` impossible to express without a second generated type.
- **Decode into the union and reject on a count mismatch.** `msgspec` exposes
  no count of discarded fields, so the information needed for the decision is
  unavailable after the decode.
- **Deep validation of nested mappings.** Reimplementing `msgspec`'s nested
  Struct rules would duplicate its semantics and disagree with
  `forbid_unknown_fields` and custom decoders.

## Consequences

An undeclared top-level field in a valid schema-tagged message now reaches
`on_unhandled` with the original frame. Applications that relied on extras
being silently dropped must remove the field or declare it, or opt into
`strict=False` on a decorated handler. Renamed fields must arrive under their
encoded wire name. Schema frames are decoded twice (generic decode for the
strict decision, then a wire decode into the member), while the tag table and
allowed-key sets are cached per schema. The tag table is resolved before the
frame is decoded, so a misconfigured schema raises `TypeError` for any frame,
including one that would otherwise fall back as malformed.
