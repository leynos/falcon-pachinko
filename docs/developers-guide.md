# Developer Guide

This guide captures maintainer-facing conventions that are not part of the
public user guide.

## Spelling policy

The tracked `typos.toml` is generated from the shared estate dictionary and
the repository-specific `typos.local.toml` overlay. Never edit generated
entries by hand. Add only narrow repository terminology to the overlay, then
generate the configuration with:

```bash
make spelling-config-write
```

Use `make spelling-config` to verify that the generated file is current. The
shared `typos-config-builder` CLI refreshes the estate dictionary into an
untracked local cache only when the authoritative copy is newer. A valid cache
remains usable when the network is unavailable. Quoted APIs and identifiers
retain their upstream spelling; put them in backticks or fenced code blocks
where practical rather than adding broad word-level exceptions.

## Router Request Boundary

`WebSocketRouter` is mounted as a Falcon resource, but its internal dispatch
pipeline only needs a small request surface before handing control to resource
callbacks:

- `path`: the concrete request path used for route matching.
- `path_template`: the Falcon mount template used to verify that the request
  belongs to the mounted router prefix.

The router models that surface with the private `_RequestLike` protocol in
`falcon_pachinko/router.py`. Internal routing helpers should accept
`_RequestLike` instead of broad `object` parameters. This keeps request-shaped
test doubles type-checkable while documenting the attributes the router
actually consumes.

Keep casts to `falcon.Request` at the edge where the router calls APIs whose
public contract is Falcon-specific, such as hook notification and
`WebSocketResource.on_connect()`. Do not widen the whole router pipeline back
to `falcon.Request` unless those internals start depending on Falcon-only
attributes.

Test request doubles should expose both `path` and `path_template`. Use an
empty `path_template` for root-mounted router tests, matching the runtime
default used by Falcon-style request objects that do not provide a template.

## Build Environment

The `build` target owns the local virtual environment. It depends on the
`.venv` target, which runs:

```sh
uv venv --clear
```

This deliberately replaces an existing `.venv` before `uv sync --group dev`.
The behaviour matches CI, where a previous step may already have created the
directory. Without `--clear`, modern `uv` exits with an error when `.venv`
exists, causing downstream gates such as `make typecheck` to fail before they
reach analysis.

Prefer Makefile targets over invoking tools directly. When changing the
Makefile, run `mbake validate Makefile` and the relevant commit gates before
committing.
