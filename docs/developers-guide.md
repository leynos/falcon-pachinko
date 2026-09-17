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

`tests/workflow_contracts/` needs two development dependencies the library
itself does not. **PyYAML** parses the GitHub Actions documents and
**Hypothesis** generates the documents the scanner is held to. Both are in the
`dev` dependency group, so `make build`, which runs `uv sync --group dev`,
installs them; `uv sync --group dev` on its own does as well. `make test`
collects the contracts, which is what makes them a gate rather than a
convenience, and `uv run pytest tests/workflow_contracts` runs them alone.
Running them without those dependencies fails at import.

The contracts scan whole parsed workflow documents rather than a list of step
keys, because a credential can be declared at workflow scope, at job scope, on
a step, or as an action input. The action marker is the exception: it is scoped
to `uses` values, because applied to every scalar it would report a step named
"check CodeScene coverage" as an invocation, and scoped to one action reference
it would miss a second CodeScene action entirely.

The upload carries a ref guard as well as its trigger. `workflow_dispatch` can
select any branch or tag, and the push trigger's `branches: [main]` says
nothing about a dispatch, so without the guard a dispatch from a feature branch
would publish that branch's coverage through the main-owned upload.

The workflow also serializes per ref and cancels nothing. The shared action
saves a fresh baseline cache per successful push and later runs restore the
newest match, so two overlapping pushes would let the older commit's baseline
become the one every pull request is measured against.

Each of the three markers is proved against a document carrying only its own
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
`delay_and_comment` and `get-codescene-sha.yml`'s `refresh-sha` are
dispatch-only. `dependabot-automerge.yml`'s `automerge` calls a reusable
workflow and declares no runner of its own.

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

Every other event, `push` to `main` included, leaves the fork field null and
reaches Ubicloud. Two rules follow, and a green run demonstrates neither:

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

*Table: the runs the ceilings are sized from.*

Fifteen minutes is roughly twenty-five times the longest of these, which leaves
room for a cold cache and for Ubicloud's two vCPUs. The release lanes have no
recorded run at all, so their thirty minutes is not measured; it is set wide on
the principle that a release killed part-way is worse than one that runs long.
Re-size on the third Ubicloud run of each lane.

The second coverage sample is the reason this repository is worth moving at
all. Its 588 s queue against a 30 s run is the queueing the migration targets;
the 19 s and 36 s lanes will be dominated by runner start-up either way.

### Contracts

`tests/workflow_contracts/` holds the rules above, and
`make test-workflow-contracts` runs them on their own. `make test` collects
them too, which is what makes them a gate rather than a convenience.

The registry in `.github/actionlint.yaml` is held to an equality with the
labels actually in use, in both directions. A subset assertion would miss a
registration that outlived its last use, which reads as a provider still in
play. The exemption is a named set of GitHub-hosted labels rather than a
provider prefix: a prefix rule would silently exempt a second paid provider's
labels, which is the question the registry exists to ask.

Two contracts read the `on:` block rather than the jobs, because the placement
rules rest on claims about events: that a fork can reach `lint-test`, and that
no fork can reach the other three. A contract reading only `runs-on` asserts
the consequence and never the premise, so a silent change to a trigger would
leave the consequence looking deliberate. Those contracts read the trigger
block under the boolean key `True`, because YAML 1.1 parses the bare word `on`
that way; a contract reading the string `"on"` finds nothing and passes on an
empty mapping, which is itself asserted.
