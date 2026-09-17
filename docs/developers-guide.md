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

## Lint and Typecheck Toolchain

`make lint` runs ruff check, then Pylint, then ambrleaks. `make typecheck`
runs `ty check falcon_pachinko tests`.

Ruff is pinned at 0.16.4 via `RUFF_VERSION` in the Makefile, and the same
version is installed in `.github/workflows/ci.yml` with
`uv tool install ruff==0.16.4`. Ruff runs in preview mode, targets py312, and
also formats Python code blocks embedded in Markdown.

`ty` is pinned at 0.0.74 via `TY_VERSION` in the Makefile, with a matching
`uv tool install ty==0.0.74` step in CI. `ty` is pre-1.0 and its diagnostics
shift between releases, which is why the version is pinned rather than left
floating.

`tests/test_toolchain_versions.py` is a contract test asserting that the
Makefile pins and the CI pins name the same version, without hard-coding a
version itself. Bump both sites together when upgrading either tool.

Pylint runs on CPython 3.14 (`PYLINT_PYTHON`), loading the
`df12-python-lints` plugin pinned by `DF12_PYTHON_LINTS_REF` (`v0.3.0`). The
plugin supplies the df12 house-style checkers. No PyPy shim is used.

`PYLINT_JOBS` derives a worker count from a tenth of the host's cores, with a
floor of two, because the build host is shared. With more than one job,
df12-python-lints v0.3.0 reports each of its own messages twice, because its
`register()` hook runs again in every worker; Pylint's builtin messages are
unaffected, and the exit status stays correct, so the gate still passes and
fails accurately. Set `PYLINT_JOBS=1` as an escape hatch when counting
findings matters. This double-counting is tracked upstream as

<https://github.com/leynos/df12-python-lints/issues/24>.

`ambrleaks`, also from df12-python-lints, runs as part of `make lint` and
scans syrupy `.ambr` snapshots for unredacted values.

### Suppression policy

Every `noqa`, `pylint: disable`, or `type: ignore` pragma must carry a reason
in the same comment. The df12-python-lints C9106 and C9107 checkers reject
bare pragmas.

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
