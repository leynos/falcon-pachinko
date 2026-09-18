# Developer Guide

This guide captures maintainer-facing conventions that are not part of the
public user guide.

## Spelling policy

Run the spelling gate with:

```bash
make spelling
```

The tracked `typos.toml` is regenerated on every run from the live shared
dictionary and the repository-specific `typos.local.toml` overlay. Never edit
generated entries by hand; add only narrow repository terminology to the
overlay. Because the dictionary is live, `typos.toml` must never be drift
checked in continuous integration.

The shared `typos-config-builder` CLI refreshes the estate dictionary into an
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
`WebSocketResource.on_connect()`. Do not widen the whole router pipeline back to
`falcon.Request` unless those internals start depending on Falcon-only
attributes.

Test request doubles should expose both `path` and `path_template`. Use an empty
`path_template` for root-mounted router tests, matching the runtime default
used by Falcon-style request objects that do not provide a template.

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

## Coverage and CodeScene

`coverage-main.yml` owns both persistent coverage outputs: the CodeScene
upload and the ratchet baseline. `ci.yml` generates coverage on a pull request
only, for its own ratchet check, and no workflow a pull request can reach names
a CodeScene action, runs a `cs-coverage` command or carries `CS_ACCESS_TOKEN`.
That separation is the estate rule CV-005.

It is not tidiness. Between 2026-09-16 and 2026-09-18 an unpinned `cs-coverage`
could not parse its own cobertura output. Because the check ran as a step of
the pull-request lane, every pull request in this repository was blocked on a
failure that had nothing to say about the change under review and that no pull
request could fix. A lane that cannot contact CodeScene cannot be stopped by
CodeScene.

Both lanes must measure the same thing, or the baseline the trunk writes is not
the baseline a pull request should be compared against.
`tests/workflow_contracts/test_codescene_coverage.py` holds their
`generate-coverage` inputs equal field by field, and holds the list of compared
fields equal to the set both lanes declare, so an input added to both and not
to the list cannot drift unnoticed. The pull-request lane declines the report
artefact; the publisher keeps the action's default and uploads what it wrote.

Both shared actions are pinned to one revision, and the upload passes no
`installer-checksum`. From shared-actions `f68e8e2e` the action pins
`cs-coverage` through its own manifest and rejects a non-empty value for that
input; `archive-checksum` replaces it. That pin is what fixed the parse break,
and a repin without the input change is a red lane rather than a warning.

The contracts scan whole parsed workflow documents rather than a list of step
keys, because a credential can be declared at workflow scope, at job scope, on
a step, or as an action input. Each of the three markers is proved against a
document carrying only its own interaction: the scan clears a workflow by
finding nothing, so a marker that had stopped matching would clear the very
thing it exists to catch while the others kept the suite green.

One consequence to retire deliberately. `get-codescene-sha.yml` refreshes the
`CODESCENE_CLI_SHA256` repository variable, and `installer-checksum` was its
only consumer. Nothing reads it now. The workflow is left in place rather than
deleted here, and a contract holds it to naming `cs-coverage` in a download URL
and nothing more: it may not acquire the credential or call the action.
