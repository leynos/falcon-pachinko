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
thing that equality exists to refuse.

A job's matrix is read only for the keys its `runs-on` names, and for each of
those both shapes are read: the axis the matrix declares as a list under the
key, and any `include` row carrying the same key. Reading every `os` in sight
would enter a test parameter named `os` into the labels in use and demand a
registration for a runner no job can request. Reading only `include` rows
would miss `matrix: {os: [ubuntu-latest, windows-latest]}` entirely, and leave
its labels unregistered.

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
