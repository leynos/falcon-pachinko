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

| Target             | Interpreter                      | Configuration        | Checks                                     |
| ------------------ | -------------------------------- | -------------------- | ------------------------------------------ |
| `make lint-pylint` | PyPy 8.0.0, Python 3.12.14 build | `pyproject.toml`     | Classic, built-in Pylint messages          |
| `make lint-df12`   | CPython 3.14                     | `pylintrc-df12.toml` | df12-python-lints checkers, then ambrleaks |

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

## Coverage and CodeScene

`coverage-main.yml` owns both persistent coverage outputs: the CodeScene
upload and the ratchet baseline. `ci.yml` generates coverage on a pull request
only, for its own ratchet check, and no workflow a pull request can reach names
a CodeScene action, runs a `cs-coverage` command, calls the service's host,
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
`tests/workflow_contracts/test_codescene_publisher.py` holds their
`generate-coverage` inputs equal field by field, and holds the list of compared
fields equal to the set both lanes declare, so an input added to both and not
to the list cannot drift unnoticed. Agreement is not enough on its own: each
lane must also set `with-ratchet: 'true'`, because turning the ratchet off in
both lanes at once keeps them equal and leaves the pull request with no coverage
gate. The pull-request lane declines the report artefact; the publisher keeps
the action's default and uploads what it wrote.

The contracts recognize a shared action by its whole path before `@`, at any
ref. A substring search accepts `someone-else/upload-codescene-coverage` as the
real upload, so a step repointed at a look-alike would satisfy every rule
written about the one it replaced.

Both shared actions are pinned to one revision, and the upload passes no
checksum input. From shared-actions `f68e8e2e` the action pins `cs-coverage`
through its own manifest, which is what fixed the parse break, and rejects a
non-empty `installer-checksum`. Its optional `archive-checksum` adds no
assurance: the action verifies the downloaded archive against the
`archive_sha256` in its own `cli-manifest.json`, so a caller-supplied digest can
only agree with that manifest or go stale and fail. A contract refuses both
inputs.

Neither lane fetches full Git history. The ratchet compares the measured
percentage with a stored baseline and reads no commits. The full clone the
pull-request lane once requested dates from the CodeScene check step, which has
left it, and `test_the_pull_request_lane_fetches_no_history` keeps it from
returning.

`tests/workflow_contracts/` needs two development dependencies the library
itself does not. **PyYAML** (`pyyaml>=6.0.3`) parses the GitHub Actions
documents and **Hypothesis** (`hypothesis>=6.168.0`) generates the documents the
scanner is held to. Both are in the `dev` dependency group, so `make build`,
which runs `uv sync --group dev`, installs them; `uv sync --group dev` on its
own does as well. `make test` collects the contracts, which is what makes them
a gate rather than a convenience, and `uv run pytest tests/workflow_contracts`
runs them alone. Running them without those dependencies fails at import.

What a pull request can reach is a closure, not a trigger list.
`tests/workflow_contracts/pull_request_reach.py` starts from every workflow a
`pull_request` or `pull_request_target` event starts, reading the trigger block
in scalar, list or mapping form under either key, and follows each job-level
call into this repository's workflow directory, recognized by where the
reference resolves rather than by a list of prefixes, GitHub's recommended `$/`
self-repository spelling included. A `workflow_call`-only
workflow a pull-request job calls runs on that pull request, and with `secrets:
inherit` it holds every secret the caller does; a call the reader cannot place
is refused rather than skipped. `test_codescene_boundary.py` scans that closure.

The contracts scan whole parsed workflow documents rather than a list of step
keys, because a credential can be declared at workflow scope, at job scope, on
a step, as an action input, or forwarded by name, and the service's host can be
curled from any shell script. `secrets: inherit` names nothing, so it is
recognized by its position. The action marker is the exception: it is scoped
to `uses` values, because applied to every scalar it would report a step named
"check CodeScene coverage" as an invocation, and scoped to one action reference
it would miss a second CodeScene action entirely.

The upload carries a ref guard as well as its trigger. `workflow_dispatch` can
select any branch or tag, and the push trigger's `branches: [main]` says
nothing about a dispatch, so without the guard a dispatch from a feature branch
would publish that branch's coverage through the main-owned upload. The
contract reads the guard as a conjunction through
`tests/workflow_contracts/guard_conditions.py` and refuses `||`, because a
substring check passes a guard with `|| github.event_name ==
'workflow_dispatch'` appended. The same module's `admits` evaluates the guard
for a push to main, dispatches on main, a branch and a tag, and a missing
token, which is the behavioural question a workflow runner would answer.

The token is bound in no `env`. The uploader is a composite action that binds
the token itself from its `access-token` input and hands a step's `env` to its
nested `upload-artifact` and cache steps. A check step with the id
`codescene-token` runs one command,
`echo "available=${{ secrets.CS_ACCESS_TOKEN != '' }}" >> "$GITHUB_OUTPUT"`,
whose expression GitHub evaluates before the shell starts, so the secret
reaches no process. The upload runs only when that output is `'true'` and the
ref is main, and passes `${{ secrets.CS_ACCESS_TOKEN }}` straight to
`access-token`. `tests/workflow_contracts/test_codescene_token.py` asserts the
exact command with no `if:` or `env`, the output conjunct, the direct input,
and no `env` anywhere in the publisher carrying the token under any name or
reading the secrets context at all, which an indexed expression such as
`secrets[format(...)]` would otherwise hide. A
guard on `env.CS_ACCESS_TOKEN != ''` would not do: with the binding deleted it
is simply false, and the upload skips forever without failing anything.

One known exception: a Dependabot pull request merged by the automerge workflow
uses `GITHUB_TOKEN`, whose merges fire no push event, so that commit publishes
no coverage until the next push or a dispatch on main.

The workflow also serializes per ref and never cancels a running generation. The
shared action saves a fresh baseline cache per successful push and later runs
restore the newest match, so two overlapping pushes would let the older commit's
baseline become the one every pull request is measured against. It is not a
durable queue: GitHub keeps one pending run per group, so a newer push replaces
an older pending one, which skips an intermediate commit no pull request should
be measured against.

The contracts read through `workflow_support.WorkflowSource`, the same source,
strict loader and error hierarchy the placement contracts use, so a repeated
key cannot hide a credential and `except UnreadableWorkflowError` catches every
reader's failure. The reusable queries live in
`tests/workflow_contracts/codescene_scan.py` and take their source explicitly.

Each marker is proved against a document carrying only its own
interaction: the scan clears a workflow by finding nothing, so a marker that
had stopped matching would clear the very thing it exists to catch while the
others kept the suite green. The reader is proved the same way, over temporary
directories, because a reader that returned an empty list for a missing
directory would clear the whole estate by finding no workflows in it.

The publisher is now the only workflow here that names CodeScene at all.
`get-codescene-sha.yml` refreshed the `CODESCENE_CLI_SHA256` repository
variable, and `installer-checksum` was its only consumer; since the shared
action rejects that input and pins the CLI through its own manifest, the
workflow maintained a value nothing read. It is deleted, and a contract refuses
any workflow that reads or refreshes the variable. The repository variable
itself can be removed whenever convenient, because nothing reads it.

## Markdown formatting and linting

`make fmt` rewrites Markdown and `make check-fmt` verifies it, both by calling
`mdtablefix` directly over `--git --include-untracked`. That selection is the
tracked Markdown set plus anything new, so a document is neither missed because
it has not been committed yet nor rewritten twice. `make fmt` also runs
`markdownlint-cli2 --fix`, because the two tools fix different things and a
contributor who ran only one would learn the rest from CI.

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

`tests/workflow_contracts/test_markdown_baseline.py` holds each of those
facts, and each is proved by removing the thing it asserts. It reads the
Makefile rather than running it, expanding variable references first: a
contract matching the literal text `$(MDLINT)` would pass with that variable
pointing anywhere at all.

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
included, carries no `pull_request` object at all, so the field is null. `false`
and null are both falsy, so both reach Ubicloud; the distinction matters only
because a reader who believed non-fork pull requests left the field null would
conclude the expression had a case it does not handle.

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
global, and the rules that prove the readers pass a directory of their own.
A rule parametrized over `.github/workflows` can only show that the current
files pass, which they do whether or not the rule discriminates anything:
a reader tied to the wrong matrix key, or blind to a direct matrix axis, would
leave every such rule green. So the readers are driven over documents written
for one question each, and each of those documents is one the estate does not
contain.

Every failure the reader can meet is translated into a named error: a
directory that cannot be listed, a file that cannot be read, one that is not
UTF-8, one that is not YAML, and one that is YAML but not a mapping. A
contract that meets any of them fails saying so, rather than surfacing an
`OSError` from inside a generator. A runner label the reader cannot parse is
raised rather than skipped, because a skipped label leaves the registry
equality holding over a smaller set than the estate uses, which is the one
thing that equality exists to refuse. The same holds one level up: a job that
is not a mapping, and a registry entry that is not a string, are refused
rather than filtered out.

Workflows are parsed with `StrictLoader` from
`tests/workflow_contracts/strict_yaml.py`, a `SafeLoader` that refuses a
mapping repeating a key and a key that is itself a list or mapping. PyYAML
alone keeps the last of two equal keys without a word, so a job declaring
`runs-on` twice could carry a paid label in the discarded half and read as
hosted to every rule here.

`runs-on` is read by `tests/workflow_contracts/runs_on_forms.py` in all three
forms GitHub accepts: a label, a list of labels, and a mapping of `group` and
`labels`, where a runner group counts as
a label because it is as billable and selects a runner the same way. Any other
shape is refused, not read as declaring no runner, because a job that declares
none drops out of the placement, ceiling and registry rules at once. A job
that calls a reusable workflow declares nothing, since the called workflow
places its own jobs.

A job's matrix is read only for the keys its `runs-on` names, and for each of
those both shapes are read: the axis the matrix declares as a list under the
key, and any `include` row carrying the same key. Reading every `os` in sight
would enter a test parameter named `os` into the labels in use and demand a
registration for a runner no job can request. Reading only `include` rows
would miss `matrix: {os: [ubuntu-latest, windows-latest]}` entirely, and leave
its labels unregistered. `tests/workflow_contracts/runner_matrix.py` holds
that reading. It follows a matrix reference only when the declaration is
exactly one `${{ matrix.<key> }}`: GitHub renders a composed declaration such
as `${{ matrix.os }}-${{ matrix.arch }}` into one label per combination, which
is none of the axis values, so the union of the axes would be the wrong answer
and the composed form is refused. An axis declared as an expression, an axis
nothing in the matrix supplies, and a value that is not a string are refused
for the same reason: the labels the job can run on would be unknown.

The ceilings and trigger filters in the tables above are restated in the
contracts rather than derived from the workflows. The rule that every Ubicloud
lane declares a ceiling says nothing about the number, so a ceiling widened to
six hours would pass it while leaving this guide's table wrong; the same holds
for `coverage-main.yml`'s `branches: [main]` and `release.yml`'s `v*.*.*`
tags, which carry the rest of the placement argument. Change a figure in a
workflow and this guide in the same commit, or the contract fails.

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
