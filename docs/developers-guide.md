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

`make lint` runs Ruff, then two Pylint passes, then ambrleaks. `make typecheck`
runs `ty check falcon_pachinko tests tools`. Both perform their complete
checks locally and in CI; neither needs a wrapper or a second manual step.

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
version itself. Bump both sites together when upgrading either tool. The same
file pins the structure of the Pylint passes described below.

### Pylint passes

Pylint runs twice, in two isolated `uv tool run` environments. Neither pass
touches the project `.venv`, whose interpreter, supported Python versions,
and test matrix are unaffected.

Table 1. Pylint passes run by `make lint`.

| Target | Interpreter | Configuration | Checks |
| --- | --- | --- | --- |
| `make lint-pylint` | PyPy 8.0.0, Python 3.12.14 build | `pyproject.toml` | Classic, built-in Pylint messages |
| `make lint-df12` | CPython 3.14 | `pylintrc-df12.toml` | df12-python-lints checkers, then ambrleaks |

The linter's interpreter and the source's Python baseline are separate
settings. Both configuration files pin `py-version = "3.12"`, so
version-sensitive checks follow the project's 3.12 baseline rather than the
host interpreter. In particular, the df12 pass runs on 3.14 without flagging
the `from __future__ import annotations` imports that baseline-3.12 modules
need: `redundant-future-annotations` (C9112) stays dormant below a 3.14
baseline.

Both passes run one Pylint worker (`--jobs=1`) and pin Pylint 4.0.8 and
`astroid` 4.0.4 through `PYLINT_VERSION` and `ASTROID_VERSION`. Pylint 4.0.8
is the newest release, and it declares `astroid>=4.0.2,<=4.1.dev0`, which
excludes `astroid` 4.3.x. The upgrade to `astroid` 4.3.1 is therefore
deferred until a Pylint release accepts it, and a test asserts that
`make lint-pylint ASTROID_VERSION=4.3.1` fails dependency resolution.

#### The classic pass on PyPy

PyPy 8.0.0 is the first PyPy release with a Python 3.12 build, and its
maintainers label that build beta quality. The pass establishes lint-toolchain
compatibility only; it says nothing about running the application on PyPy.

The pass needs no shim. PyPy 7.3.22 raised `TypeError` on
`types.FunctionType.__text_signature__`
([pypy/pypy#5458](https://github.com/pypy/pypy/issues/5458)), which crashed
the `astroid` bootstrap on every run; the estate's pylint-pypy-shim existed
to work around that. The bug was fixed twice: PyPy stopped raising from
7.3.23, and `astroid` 4.3.0 learned to tolerate it. This repository relies on
the PyPy fix, so vanilla `astroid` 4.0.4 bootstraps and inspects live objects
on PyPy 8.0.0. The `astroid` fix, and its related `__class_getitem__`
robustness fix, arrive with the deferred 4.3.1 upgrade. This repository never
used the shim, so the PyPy pass is an initial adoption rather than a shim
removal.

uv's built-in interpreter catalogue does not yet list PyPy 3.12, so
`tools/pypy-downloads.json` names the official PyPy 8.0.0 tarballs and their
published SHA-256 sums for Linux (x86-64 and AArch64, glibc 2.28 or newer) and
macOS (Apple silicon and Intel). `make lint-pylint` passes that manifest to
uv, which refuses any download that does not match, installs the interpreter
into `.uv-python` rather than uv's shared store, and keeps it off `PATH` with
`--no-bin`. Other platforms, including Windows and musl-based Linux, fail with
uv's "No download found" error.

To use another interpreter, set `PYLINT_PYTHON` to its path. Before Pylint
runs, `tools/check_lint_runtime.py` inspects the interpreter inside the tool
environment and rejects anything other than PyPy 8.0.0 on Python 3.12 with
the pinned Pylint and `astroid`, whether or not the variable was overridden.
The df12 pass runs the same check for CPython 3.14.

#### Failed analysis fails the gate

Both configuration files enable `syntax-error` and the fatal diagnostics
(`fatal`, `astroid-error`, `parse-error`, `config-parse-error`, and
`method-check-failed`) by name. Pylint reports these even under
`disable = ["all"]`, but an explicit disable of any of them lets an
unparsable or unanalysable module pass with exit status 0. The contract
tests reject any configuration or Makefile command that disables them.

#### Plugin isolation and pragma validation

Only `pylintrc-df12.toml` loads `df12_python_lints`, so the PyPy pass cannot
inherit the plugin through shared configuration, and its tool environment
does not contain the plugin at all. Inline pragmas such as
`# pylint: disable=trivial-attribute-wrapper` name df12 messages that the
classic pass never registers, so that pass disables `unknown-option-value`
(W0012). The df12 pass registers both the core and the df12 messages and
enables W0012, so every pragma name is still checked for typos exactly once.

#### State and caches

Each pass keeps its persisted Pylint state in its own directory under
`.cache/pylint`, named for its runtime, so neither reads the other's
statistics. CI caches `.uv-python` under a key derived from
`tools/pypy-downloads.json`, and the lint tool environments in `.uv-cache`
under a key that also covers the Makefile and `pylintrc-df12.toml`, so a pin
change never restores a stale toolchain.

#### Coverage and tests

`PYLINT_TARGETS` covers `falcon_pachinko` (listing its `unittests` and
`behaviour` directories explicitly, because they carry no `__init__.py`),
`tests`, `examples`, and `tools`, which is every tracked Python file. None
needs syntax newer than Python 3.12, so no separate CPython classic pass
exists. The three inline script blocks under `examples` declare dependencies
but no Python version.

`tests/test_lint_toolchain_integration.py` (marker `lint_toolchain`) runs the
Makefile's own commands against the real interpreters. The tests provision
PyPy on their first run, so they need network access once, and they never
skip. CI runs them as a dedicated step after `make lint`.

`ambrleaks`, also from df12-python-lints, runs at the end of `make lint-df12`
and scans syrupy `.ambr` snapshots for unredacted values.

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
