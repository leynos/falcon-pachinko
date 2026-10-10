# Developer Guide

This guide captures maintainer-facing conventions that are not part of the
public user guide.

## WorkerController lifecycle

`WorkerController.start()` rejects a second start while tasks remain registered
and schedules each worker with the same keyword context. If a worker factory or
task creation fails, it closes any coroutine rejected by
`asyncio.create_task()`, cancels and awaits tasks already scheduled, and resets
the controller so the caller can retry. The original startup exception remains
primary; rollback failures or cancellation are attached to it as exception
notes. Rollback continues cleanup if the caller is cancelled. The controller
does not keep an async exit stack; the application lifespan handler owns
external resources.

`_schedule_worker()` creates the worker task and closes a rejected coroutine.
`_rollback_start()` waits for `_finish_startup_rollback()` despite caller
cancellation; that helper cancels and awaits scheduled tasks, then clears state.
`_rollback_start_preserving_error()` keeps the startup exception primary and
attaches rollback failures or cancellation as notes.

During `stop()`, the controller cancels all tasks, gathers them, and selects
the first non-cancellation exception in registration order. It then clears the
task list and re-raises that exception, if present, leaving the controller
ready to restart. The controller emits neither startup logs nor metrics and
remains telemetry-agnostic. It propagates the original startup exception to the
application startup caller, leaving application-specific logging and metrics to
the lifespan owner.

## Dependency bounds

The package supports Falcon 4.x through `falcon>=4,<5`. Falcon follows
[semantic versioning](https://falcon.readthedocs.io/en/stable/community/releases.html#semantic-versioning),
so an incompatible API change can arrive with a new major release. This
package uses Falcon's ASGI application and WebSocket APIs in
`falcon_pachinko/router.py`, `falcon_pachinko/websocket.py`, and
`falcon_pachinko/testing/harness.py`; the exclusive upper bound keeps a future
major release out until compatibility is verified.

The lower bound was checked on 2026-10-08 UTC (2026-10-09 in Europe/Berlin) by
installing Falcon 4.0.0 with `uv pip install falcon==4.0.0` and running
`uv run --no-sync pytest -v`: 770 tests passed and 2 were skipped. Run
`make build` afterwards to restore the normal environment. The latest CI build
checked for this change resolved Falcon 4.4.0.

The repository has no committed lock file, and `.gitignore` excludes `uv.lock`.
CI therefore resolves the newest release allowed by the dependency bounds on
each build. Lower-bound coverage in CI and the decision to commit a lock file
are tracked separately from this support policy.

## Validation error import boundary

Use `from falcon_pachinko.utils import ValidationError` as the stable import
path for validation exceptions. New package code must raise through
`falcon_pachinko.utils.ValidationError`, keeping raise sites behind the
package's stable path. It is the same class as `msgspec.ValidationError`,
preserving existing exception handlers. The dispatcher routes validation
failures to `on_unhandled`; a `ValidationError` raised inside a handler still
propagates.

The typed alias is a direct assignment to preserve the original class object.
Ruff bans `from msgspec import ...`; PEP 695 syntax would not preserve class
identity. The line-local Pylint `prefer-type-statement` and Ruff
`non-pep695-type-alias` suppressions document those constraints.

## Safe diagnostics

Framework diagnostics must not call `repr()` or `str()` on raw payloads,
decoded caller values, or other caller-controlled objects. Use the shared
bounded metadata helpers in `falcon_pachinko.diagnostics` for identifiers,
types, frame kind and length, and safe scalar fields. Payload values stay
available to runtime code; their default representations, errors, logs and
trace summaries omit them.

When translating a vendor decode failure, capture only its exception class in
the `except` suite and raise the framework error after leaving that suite.
`raise ... from None` suppresses traceback display of the preceding exception,
but it does not remove the implicit `__context__` reference. Raising afterwards
avoids retaining the vendor exception and its message in either chain
attribute. Preserve application exceptions when they are propagated; diagnostic
display suppression does not sanitize an exception object retained or logged by
application code.

Application hooks and handlers that read `HookContext.raw`, fallback raw
arguments, or `TraceEvent.payload` cross a trusted boundary. Do not use payload
`repr()` or `str()` for diagnostics there; UTF-8 decoding changes
representation but does not remove secrets.

## Spelling policy

Run the spelling gate with:

```bash
make spelling
```

`TYPOS_CONFIG_BUILDER_VERSION` in the `Makefile` pins the
`typos-config-builder` release the gate runs (currently `v0.1.3`). Raise it
together with the regenerated `typos.toml`, never on its own.

The tracked `typos.toml` is regenerated on every run from the live shared
dictionary and the repository-specific `typos.local.toml` overlay. Never edit
generated entries by hand; add only narrow repository terminology to the
overlay. Because the dictionary is live, `typos.toml` must never be drift
checked in continuous integration (CI).

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

### URI-template compiler boundary

`falcon_pachinko/_uri_template.py` is the private shared compiler for full-path
and prefix matchers. The router uses both forms for top-level routes, while
nested subroutes use the prefix form. The compiler escapes every literal run
before building the regular expression, and validates placeholder braces,
unique Python-identifier names, and parameter layouts first. Placeholders
capture non-empty path segments; a segment may contain one placeholder or a
terminal adjacent pair, while other multi-placeholder layouts are ambiguous.
Compilation happens before router registration state is mutated, so invalid
templates fail at registration or mount time with `ValueError` rather than
being deferred to connection dispatch.

`uvicorn>=0.30,<1` is in the development dependency group for
`tests/test_live_routing.py`, which exercises the router through a live Falcon
ASGI server and real WebSocket connections.

### `_RouteMatch` and `_Dispatch`

`_RouteMatch` holds what the prefix match produced: `params: dict[str, str]` and
`remaining: str`, the path segment still to be consumed. As
`_resolve_subroutes` walks a resource's nested subroutes, it updates the same
`_RouteMatch` in place, consuming `remaining` and merging each subroute's
captured parameters into `params`.

`_Dispatch` is a mutable, per-connection-attempt bundle of `route`, `req`,
`ws`, and `match`. It is built once in `_try_route` and threaded through
`_execute_route_with_error_handling`, resource and subroute resolution, hook
notification (`_prepare_connection_context` fires `before_connect`;
`_finalize_connection`, and `_execute_resource_handler` on error, fire
`after_connect`), the resource's `on_connect` handler
(`_execute_resource_handler`), and finalization (`_finalize_connection`).
Bundling these fields means a step that changes route state — subroute
resolution, or a hook that rewrites `params` — is visible to every later step,
rather than requiring `route`, `req`, `ws`, `params`, and `remaining` to be
threaded positionally through each helper.

`req` is cast to `falcon.Request` at the point `_Dispatch` is constructed in
`_try_route`, consistent with the cast-at-the-edge policy described above.

## Lint and Typecheck Toolchain

`make lint` runs Ruff, then two Pylint passes, then ambrleaks. `make typecheck`
runs `ty check falcon_pachinko tests tools`. Both perform their complete checks
locally and in CI, and need no wrapper and no second manual step.

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

### Import conventions

Import `msgspec.inspect` as `msinspect`:

```python
import msgspec as ms
import msgspec.inspect as msinspect
import msgspec.json as msjson

msinspect.type_info(int)
```

Ruff rejects member imports such as `from msgspec.inspect import type_info` with
`ICN003`; it rejects a missing or different `msgspec.inspect` alias with
`ICN001`. The `[tool.ruff.lint.flake8-import-conventions]` table in
`pyproject.toml` is the authoritative source for aliases and `banned-from`.
Change that table, not this guide, to alter enforcement. When the configured
convention changes, update this guide to match.

### Pylint passes

Pylint runs twice, in two isolated `uv tool run` environments. Neither pass
touches the project `.venv`, whose interpreter, supported Python versions, and
test matrix are unaffected.

Table 1. Pylint passes run by `make lint`.

| Target             | Interpreter                      | Configuration        | Checks                                     |
| ------------------ | -------------------------------- | -------------------- | ------------------------------------------ |
| `make lint-pylint` | PyPy 8.0.0, Python 3.12.14 build | `pyproject.toml`     | Classic, built-in Pylint messages          |
| `make lint-df12`   | CPython 3.14                     | `pylintrc-df12.toml` | df12-python-lints checkers, then ambrleaks |

The linter's interpreter and the source's Python baseline are separate
settings. Both configuration files pin `py-version = "3.12"`, so
version-sensitive checks follow the project's 3.12 baseline rather than the
host interpreter. In particular, the df12 pass runs on 3.14 without flagging the
`from __future__ import annotations` imports that baseline-3.12 modules need:
`redundant-future-annotations` (C9112) stays dormant below a 3.14 baseline.

Both passes run one Pylint worker (`--jobs=1`) and pin Pylint 4.0.8 and
`astroid` 4.0.4 through `PYLINT_VERSION` and `ASTROID_VERSION`. Pylint 4.0.8 is
the newest release, and it declares `astroid>=4.0.2,<=4.1.dev0`, which excludes
`astroid` 4.3.x. The upgrade to `astroid` 4.3.1 is therefore deferred until a
Pylint release accepts it, and a test asserts that
`make lint-pylint ASTROID_VERSION=4.3.1` fails dependency resolution.

The df12 pass additionally pins `df12-python-lints` 0.3.0 through
`DF12_PYTHON_LINTS_VERSION`, installed from the `v0.3.0` tag of the
`df12-python-lints` repository. The runtime check refuses to run if any other
version of `df12-python-lints` is installed.

#### The classic pass on PyPy

PyPy 8.0.0 is the first PyPy release with a Python 3.12 build, and its
maintainers label that build beta quality. The pass establishes lint-toolchain
compatibility only; it says nothing about running the application on PyPy.

The pass needs no shim. PyPy 7.3.22 raised `TypeError` on
`types.FunctionType.__text_signature__`
([pypy/pypy#5458](https://github.com/pypy/pypy/issues/5458)), which crashed the
`astroid` bootstrap on every run; the estate's pylint-pypy-shim existed to
work around that. The bug was fixed twice: PyPy stopped raising from 7.3.23, and
`astroid` 4.3.0 learned to tolerate it. This repository relies on the PyPy
fix, so vanilla `astroid` 4.0.4 bootstraps and inspects live objects on PyPy
8.0.0. The `astroid` fix, and its related `__class_getitem__` robustness fix,
arrive with the deferred 4.3.1 upgrade. This repository never used the shim, so
the PyPy pass is an initial adoption rather than a shim removal.

uv's built-in interpreter catalogue does not yet list PyPy 3.12, so
`tools/pypy-downloads.json` names the official PyPy 8.0.0 tarballs and their
published SHA-256 sums for Linux (x86-64 and AArch64, glibc 2.28 or newer) and
macOS (Apple silicon and Intel). `make lint-pylint` passes that manifest to uv,
which refuses any download that does not match, installs the interpreter into
`.uv-python` rather than uv's shared store, and keeps it off `PATH` with
`--no-bin`. Other platforms, including Windows and musl-based Linux, fail with
uv's "No download found" error.

To use another interpreter, set `PYLINT_PYTHON` to its path. Before Pylint runs,
`tools/check_lint_runtime.py` inspects the interpreter inside the tool
environment and rejects anything other than PyPy 8.0.0 on Python 3.12 with the
pinned Pylint and `astroid`, whether or not the variable was overridden. The
df12 pass runs the same check for CPython 3.14.

#### Failed analysis fails the gate

Both configuration files enable `syntax-error` and the fatal diagnostics
(`fatal`, `astroid-error`, `parse-error`, `config-parse-error`, and
`method-check-failed`) by name. Pylint reports these even under
`disable = ["all"]`, but an explicit disable of any of them lets an unparsable
or unanalysable module pass with exit status 0. The contract tests reject any
configuration or Makefile command that disables them.

#### Plugin isolation and pragma validation

Only `pylintrc-df12.toml` loads `df12_python_lints`, so the PyPy pass cannot
inherit the plugin through shared configuration, and its tool environment does
not contain the plugin at all. Inline pragmas such as
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
Makefile's own commands against the real interpreters. The tests provision PyPy
on their first run, so they need network access once, and they never skip. CI
runs them as a dedicated step after `make lint`.

`ambrleaks`, also from df12-python-lints, runs at the end of `make lint-df12`
and scans syrupy `.ambr` snapshots for unredacted values.

### Suppression policy

Every `noqa`, `pylint: disable`, or `type: ignore` pragma must carry a reason
in the same comment. The df12-python-lints C9106 and C9107 checkers reject bare
pragmas.

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

`coverage-main.yml` owns both persistent coverage outputs: the CodeScene upload
and the ratchet baseline. `ci.yml` generates coverage on a pull request only,
for its own ratchet check, and no workflow a pull request can reach names a
CodeScene action, runs a `cs-coverage` command, calls the service's host,
carries `CS_ACCESS_TOKEN`, or forwards every secret with `secrets: inherit`.
That separation is the estate rule CV-005.

It is not tidiness. Between 2026-09-16 and 2026-09-18 an unpinned `cs-coverage`
could not parse its own cobertura output. Because the check ran as a step of
the pull-request lane, every pull request in this repository was blocked on a
failure that had nothing to say about the change under review and that no pull
request could fix. A lane that cannot contact CodeScene cannot be stopped by
CodeScene.

Both lanes must measure the same thing, or the baseline the trunk writes is not
the baseline a pull request should be compared against.

The shared CV-005 contract library holds that shape. `make check-cv005` runs
`cv005-contracts check`, from the `leynos/shared-actions` package
`packages/cv005-contracts`, at the full commit named by `CV005_CONTRACTS_REF`
in the Makefile, and CI runs it as its own step. A fix to the rules is a pin
bump. The target needs `uv`, which fetches the Python 3.13 the library runs
under, and `make test-workflow-contracts` runs it before this repository's own
contracts. The library's suite proves each rule in both directions, so this
repository keeps no copy of the readers or the refusal cases. What it holds:

- No workflow a pull request can reach names a CodeScene action or host, runs
  `cs-coverage`, carries `CS_ACCESS_TOKEN` at any scope, or forwards every
  secret with `secrets: inherit`. The reach is a closure through local calls,
  not a trigger list, and a call the reader cannot place is refused.
- The publisher uploads rather than checks, behind a ref guard read as a
  conjunction (a `||` is refused) and a check step whose one command is
  `echo "available=${{ secrets.CS_ACCESS_TOKEN != '' }}" >> "$GITHUB_OUTPUT"`.
  The token reaches `access-token` directly and is bound in no `env`, because
  the uploader is a composite action that hands a step's `env` to its nested
  steps. A guard on `env.CS_ACCESS_TOKEN != ''` would not do: with the binding
  deleted it is simply false, and the upload skips forever without failing
  anything.
- The publisher job declares `environment: codescene`, which admits deployments
  from `main` alone, and no other job does (`environment.placement`).
- The publisher's concurrency group is keyed on the ref and never cancels a
  running generation. The shared action saves a fresh baseline cache per
  successful push and later runs restore the newest match, so two overlapping
  pushes would let the older commit's baseline become the one every pull
  request is measured against. It is not a durable queue: GitHub keeps one
  pending run per group, so a newer push replaces an older pending one.
- Both lanes select the same `generate-coverage` inputs, both set
  `with-ratchet: 'true'`, and both use one shared-actions revision. The
  publisher's selection is pinned in `.github/cv005.toml` under `[selection]`,
  so a change made to both lanes at once is still a reviewed change. The
  pull-request lane declines the report artefact and the publisher keeps the
  action's default.
- The upload passes no checksum input. From shared-actions `f68e8e2e` the
  action pins `cs-coverage` through its own manifest and rejects a non-empty
  `installer-checksum`, and a workflow that reads or refreshes the retired
  `CODESCENE_CLI_SHA256` variable is refused. `get-codescene-sha.yml` is
  deleted for that reason, and the repository variable can be removed whenever
  convenient, because nothing reads it.

`.github/cv005.toml` carries this repository's parameters: `repository`, the
`[selection]` and `interpreter = "3.13"`. The interpreter makes the library
require `UV_PYTHON: '3.13'` on every `generate-coverage` step, set on the job
in both lanes, and hold that version inside `requires-python` (`>=3.12`),
because generate-coverage otherwise takes the `python3` that the latest
`setup-python` put on `PATH`, and `uv sync` refuses an interpreter outside the
range. The ratchet baseline key carries the interpreter
(`ratchet-baseline-<os>-py<major.minor>-`), so a lane on another Python would
miss its baseline rather than compare against the wrong one.

`tests/workflow_contracts/test_check_cv005_target.py` holds the target's wiring
(the pinned full commit, Python 3.13, the arguments, the parameters file and
failure propagation) without the network, and CI runs the real command as the
end-to-end check. The runner-label and lane-trigger contracts
(`test_runner_placement.py`, `test_lane_triggers.py` and their readers) stay
local too.

One fact remains this repository's own, in
`tests/workflow_contracts/test_coverage_lane_facts.py`. The pull-request lane
fetches no full Git history: the ratchet compares the measured percentage with
a stored baseline and reads no commits, and the full clone the lane once
requested dates from the CodeScene check step, which has left it. The library
has no clause for it, so the contract is proved over temporary sources as well
as this repository's workflow.

One known exception: a Dependabot pull request merged by the automerge workflow
uses `GITHUB_TOKEN`, whose merges fire no push event, so that commit publishes
no coverage until the next push or a dispatch on main.

`tests/workflow_contracts/` needs two development dependencies the library
itself does not. **PyYAML** (`pyyaml>=6.0.3`) parses the GitHub Actions
documents and **Hypothesis** (`hypothesis>=6.168.0`) generates the documents
the label readers are held to. Both are in the `dev` dependency group, so
`make build`, which runs `uv sync --group dev`, installs them;
`uv sync --group dev` on its own does as well. `make test` collects the
contracts, which is what makes them a gate rather than a convenience, and
`uv run pytest tests/workflow_contracts` runs them alone. Running them without
those dependencies fails at import.

## Markdown formatting and linting

`make fmt` rewrites Markdown and `make check-fmt` verifies it, both by calling
`mdtablefix` directly over `--git --include-untracked`. That selection is the
tracked Markdown set plus anything new, so a document is neither missed because
it has not been committed yet nor rewritten twice. Both targets also pass
`$(MDTABLEFIX_RULES)`, which is
`--wrap --renumber --breaks --ellipsis --fences`: without those flags
`check-fmt` would pass files that `fmt` still wraps or renumbers, and the
80-column rule would go unenforced. The workflow contract asserts every one of
the flags in both targets, in the Makefile and in what Make expands and runs.
`make fmt` also runs `markdownlint-cli2 --fix`, because the two tools fix
different things and a contributor who ran only one would learn the rest from
CI.

The rules and exclusions live in `.markdownlint-cli2.jsonc`, copied verbatim
from the estate canon. A repository may add rules and globs alongside them; it
may not weaken one. An alternate file name does not count, because
`markdownlint-cli2` reads several and a repository carrying two would enforce
whichever it found first.

CI lints Markdown through `DavidAnson/markdownlint-cli2-action` pinned to a
full commit SHA, and nothing else may. The action's release carries the
linter's whole dependency graph, so nothing is resolved from the npm registry
while the gate is running: a moved tag or a registry outage cannot change what
the gate enforces without changing this repository. A `run:` step that invokes
the linter, or drives `make markdownlint`, is the defect this replaces.

Before this, CI installed `markdownlint-cli2` and then linted no Markdown at
any point, and the Makefile named `markdownlint`, which is a different program.
It resolved to nothing, so the local target ran `xargs` with no command and
exited zero having linted the empty set. A gate that reports success while
checking nothing is worse than no gate, because it is believed.

`tests/workflow_contracts/test_markdown_baseline.py` holds each of those facts,
and each is proved by removing the thing it asserts. It reads the Makefile
rather than running it, expanding variable references first: a contract
matching the literal text `$(MDLINT)` would pass with that variable pointing
anywhere at all.

## GitHub Actions runner placement

Repository-owned Linux lanes run on Ubicloud managed runners. Scheduled,
dispatch-only and `pull_request_target` work stays GitHub-hosted, where
public-repository minutes are free and where this migration has nothing to gain.

| Job                                   | Trigger            | Ceiling |
| ------------------------------------- | ------------------ | ------- |
| `ci.yml` `lint-test`                  | pull request, push | 15 min  |
| `coverage-main.yml` `coverage-upload` | push to `main`     | 15 min  |
| `release.yml` `pure-wheel`            | tag push           | 30 min  |
| `release.yml` `release`               | tag push           | 30 min  |

*Table: the lanes that run on Ubicloud.*

Everything else stays GitHub-hosted. `build-wheels.yml`'s `build` is a
`workflow_call` matrix across Ubuntu, Windows and macOS, including aarch64
under emulation, and Ubicloud offers Linux only. `delayed-pr-comment.yml`'s
`delay_and_comment` is dispatch-only. `dependabot-automerge.yml`'s `automerge`
calls a reusable workflow and declares no runner of its own.

### The pull-request lane's label is an expression

A pull request from a fork cannot obtain an Ubicloud runner. `lint-test` serves
pull requests, so a fixed `ubicloud-standard-2` would leave such a pull request
queueing for a runner that never arrives. It resolves its label from the head
repository instead:

```yaml
runs-on: >-
  ${{ github.event.pull_request.head.repo.fork
  && 'ubuntu-latest' || 'ubicloud-standard-2' }}
```

The guard reads one field, and it takes three values. A pull request from a
fork sets it to `true` and takes the GitHub-hosted arm. A pull request from a
branch of this repository sets it to `false`. Any other event, `push` to `main`
included, carries no `pull_request` object at all, so the field is null.
`false` and null are both falsy, so both reach Ubicloud; the distinction
matters only because a reader who believed non-fork pull requests left the
field null would conclude the expression had a case it does not handle.

Two rules follow, and a green run demonstrates neither:

- Keep the continuation at the same indent as the first line. A continuation
  indented one level deeper keeps its line break, so the folded scalar parses
  to a label with a newline inside the expression. The document still parses,
  `actionlint` still passes, and GitHub evaluates the newline-bearing
  expression anyway.
- Do not copy the expression onto the other three lanes. They trigger on a
  push or a tag, where `github.event.pull_request` is null, so the guard could
  never hold. A guard that cannot hold reads as protection and is not.

### Ceilings

An Ubicloud runner registers as a just-in-time self-hosted runner, so GitHub's
six-hour cap does not apply and an unbounded lane can run to the five-day
limit. Every lane that can reach Ubicloud therefore declares `timeout-minutes`.

The two measured ceilings are sized from real runs rather than chosen:

| Run                               | Job               | Queue | Run time |
| --------------------------------- | ----------------- | ----- | -------- |
| CI, pull request, 2026-09-17      | `lint-test`       | 2 s   | 36 s     |
| CI, push, 2026-09-16              | `lint-test`       | 3 s   | 19 s     |
| Coverage (main), push, 2026-09-16 | `coverage-upload` | 3 s   | 34 s     |
| Coverage (main), push, 2026-08-13 | `coverage-upload` | 588 s | 30 s     |
| CI, PR, 2026-09-17, Ubicloud      | `lint-test`       | 11 s  | 44 s     |

*Table: the runs the ceilings are sized from.*

Fifteen minutes is roughly twenty-five times the longest of these, which leaves
room for a cold cache and for Ubicloud's two vCPUs. The release lanes have no
recorded run at all, so their thirty minutes is not measured; it is set wide on
the principle that a release killed part-way is worse than one that runs long.
Re-size on the third Ubicloud run of each lane.

The last row is the first run on Ubicloud, and it is slower than the
GitHub-hosted run above it: 44 s against 36 s, on an 11 s queue against 2 s.
One sample decides nothing, but it is what the paragraph below predicts, and it
is recorded rather than quietly dropped.

The second coverage sample is the reason this repository is worth moving at
all. Its 588 s queue against a 30 s run is the queueing the migration targets;
the 19 s and 36 s lanes will be dominated by runner start-up either way.

### Contracts

`tests/workflow_contracts/` holds the rules above, and
`make test-workflow-contracts` runs them on their own. `make test` collects
them too, which is what makes them a gate rather than a convenience.

They need two development dependencies that the library itself does not.
**PyYAML** parses the GitHub Actions documents, and **Hypothesis** generates
the declarations the label readers are held to. Both are in the `dev`
dependency group, so `make build`, which runs `uv sync --group dev`, installs
them; `uv sync --group dev` on its own does as well. Running
`make test-workflow-contracts` or `make all` without them fails at import.

The readers take the directory they read rather than reaching for a module
global, and the rules that prove the readers pass a directory of their own. A
rule parametrized over `.github/workflows` can only show that the current files
pass, which they do whether or not the rule discriminates anything: a reader
tied to the wrong matrix key, or blind to a direct matrix axis, would leave
every such rule green. So the readers are driven over documents written for one
question each, and each of those documents is one the estate does not contain.

Every failure the reader can meet is translated into a named error: a directory
that cannot be listed, a file that cannot be read, one that is not UTF-8, one
that is not YAML, and one that is YAML but not a mapping. A contract that meets
any of them fails saying so, rather than surfacing an `OSError` from inside a
generator. A runner label the reader cannot parse is raised rather than
skipped, because a skipped label leaves the registry equality holding over a
smaller set than the estate uses, which is the one thing that equality exists
to refuse. The same holds one level up: a job that is not a mapping, and a
registry entry that is not a string, are refused rather than filtered out.

Workflows are parsed with `StrictLoader` from
`tests/workflow_contracts/strict_yaml.py`, a `SafeLoader` that refuses a
mapping repeating a key and a key that is itself a list or mapping. PyYAML
alone keeps the last of two equal keys without a word, so a job declaring
`runs-on` twice could carry a paid label in the discarded half and read as
hosted to every rule here.

`runs-on` is read by `tests/workflow_contracts/runs_on_forms.py` in all three
forms GitHub accepts: a label, a list of labels, and a mapping of `group` and
`labels`, where a runner group counts as a label because it is as billable and
selects a runner the same way. Any other shape is refused, not read as
declaring no runner, because a job that declares none drops out of the
placement, ceiling and registry rules at once. A job that calls a reusable
workflow declares nothing, since the called workflow places its own jobs.

A job's matrix is read only for the keys its `runs-on` names, and for each of
those both shapes are read: the axis the matrix declares as a list under the
key, and any `include` row carrying the same key. Reading every `os` in sight
would enter a test parameter named `os` into the labels in use and demand a
registration for a runner no job can request. Reading only `include` rows would
miss `matrix: {os: [ubuntu-latest, windows-latest]}` entirely, and leave its
labels unregistered. `tests/workflow_contracts/runner_matrix.py` holds that
reading. It follows a matrix reference only when the declaration is exactly one
`${{ matrix.<key> }}`: GitHub renders a composed declaration such as
`${{ matrix.os }}-${{ matrix.arch }}` into one label per combination, which is
none of the axis values, so the union of the axes would be the wrong answer and
the composed form is refused. An axis declared as an expression, an axis
nothing in the matrix supplies, and a value that is not a string are refused
for the same reason: the labels the job can run on would be unknown.

The ceilings and trigger filters in the tables above are restated in the
contracts rather than derived from the workflows. The rule that every Ubicloud
lane declares a ceiling says nothing about the number, so a ceiling widened to
six hours would pass it while leaving this guide's table wrong; the same holds
for `coverage-main.yml`'s `branches: [main]` and `release.yml`'s `v*.*.*` tags,
which carry the rest of the placement argument. Change a figure in a workflow
and this guide in the same commit, or the contract fails.

The registry in `.github/actionlint.yaml` is held to an equality with the
labels actually in use, in both directions. A subset assertion would miss a
registration that outlived its last use, which reads as a provider still in
play. The exemption is a named set of GitHub-hosted labels rather than a
provider prefix: a prefix rule would silently exempt a second paid provider's
labels, which is the question the registry exists to ask.

The in-use set accounts for every workflow that declares a label, including
`build-wheels.yml`, which is `workflow_call` and which nothing in this
repository calls. The alternative was a rule excluding callerless
`workflow_call` files, and it was rejected: such a file is one line away from
gaining a caller, and "callerless" is not even a local property, since a caller
may live in another repository. A rule that skipped it would quietly stop
asking the registry question for a workflow that could run tomorrow. A contract
names the workflows the traversal must reach, so dropping one fails there
rather than silently shrinking what the registry is held to.

Two contracts read the `on:` block rather than the jobs, because the placement
rules rest on claims about events: that a fork can reach `lint-test`, and that
no fork can reach the other three. A contract reading only `runs-on` asserts
the consequence and never the premise, so a silent change to a trigger would
leave the consequence looking deliberate. Those contracts read the trigger
block under the boolean key `True`, because YAML 1.1 parses the bare word `on`
that way; a contract reading the string `"on"` finds nothing and passes on an
empty mapping, which is itself asserted.
