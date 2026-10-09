# Adopt the nose duplication gate

## Why this work matters

Repeated production logic can drift while tests and linters still pass. This
change adds a deterministic, blocking check that makes substantial duplication
visible and keeps justified parallel implementations reviewable. Success is
observable when `make lint` runs the pinned detector, rejects an unallowed
planted clone, accepts a complete reasoned exception, and rejects a new family
member outside that exception.

## Conformance basis

The detector decision and support-code baseline are `leynos/episodic` PR #276
at immutable commit `d9e5ac0d254f375e2986f52d91a3b88c117c833b`, especially
ADR-021 and the merged nose gate at that revision. Upstream command and JSON
behaviour is taken from `corca-ai/nose` v0.20.0. This repository has no
existing ADR series; its maintainer-facing source of truth is
`docs/developers-guide.md`.

The delivery maps the requirements to these observable results:

- `GATE-SCOPE`: `[tool.nose]` names existing shipped Python roots, all three
  channels, the 24-token floor, `surface = "all"` and the explicit top-30
  ranking limit; a root that selects no production Python fails closed.
- `GATE-REPORT`: one repository-rooted JSON query validates and
  deterministically reports every selected family, with exit status 0, 1 or 2
  for pass, blocking duplication or configuration/execution failure.
- `GATE-ALLOW`: one reasoned TOML entry must cover every family location;
  persistence preserves comments, permissions and concurrent updates.
- `GATE-TOOLS`: only the pinned official nose v0.20.0 release archive for the
  detected POSIX platform is accepted after its platform-specific SHA-256 is
  checked; gate and helper tests use CPython 3.14 without installing the
  application.
- `GATE-INTEGRATION`: `make lint` and Linux CI run the gate, while a separate
  isolated target runs focused helper tests. Application tests do not collect
  Python-3.14-only helper tests.
- `GATE-ADJUDICATION`: each selected family is inspected in context; genuine
  repeated logic is refactored with regression coverage, while intentional
  parallels receive a narrow, reasoned exception. A disposable unsaturated
  fixture proves clean, blocking, allowed and out-of-scope-member behaviour.
- `GATE-DOCUMENTATION`: the developer guide, agent instructions and README
  entrypoint explain scope, limits, provisioning, exception review and stale
  entries; this plan records the adoption evidence and deviations.

## Repository orientation

`pyproject.toml` sets the application floor to Python 3.12 and explicitly
packages only `falcon_pachinko`. Its direct modules are the maintained shipped
surface. The nested `falcon_pachinko/testing`, `falcon_pachinko/unittests` and
`falcon_pachinko/behaviour` trees are not in that package list and are excluded
from the detector. Top-level `tests/`, `examples/` and `tools/` are also
excluded: they contain application tests, demonstration applications and
maintenance tooling, respectively. No separate `src/` root or shipped top-level
module was found. There is no existing atomic-write helper suitable for
cross-process allowlist updates.

The canonical lint entrypoint is `make lint`; formatting, type checking,
documentation checks and application tests are separate Make targets. CI runs
on Linux, the application test interpreter is CPython 3.13, and the existing
lint toolchain already provisions CPython 3.14. The unchanged persistence
helper uses POSIX locking and directory operations, so native Windows gate
execution is not claimed; Windows developers can use WSL. The application
platform matrix remains untouched.

## Decisions and constraints

The scan starts at `falcon_pachinko` and explicitly excludes only the nested
non-production package trees identified above. It pins `syntax,semantic,near`,
`min-size = 24`, `surface = "all"` and `top = 30`. The token floor measures
nose intermediate-language tokens. The top 30 ranked families are selected
before exceptions are applied; allowed families consume places, and lower
ranked families are not enforced by this gate. The semantic channel supplies
limited witness-backed evidence, not a general Type-4 clone guarantee.

The installer will download only the official, versioned GitHub release archive
and verify its platform-specific published SHA-256 before extraction. This
deliberately replaces the reference's cargo-binstall Git-source installer: the
release provides checksummed binaries for the supported Linux and macOS
architectures, so a second bootstrap binary is unnecessary. There is no
compilation fallback. Missing platform assets or digests fail with an
actionable error.

The application package list, Python floor, runtime dependencies, supported
platforms and public behaviour are outside the change. No PyChase, pyscn,
benchmark artefacts, episodic allow entries or estate-wide framework are
introduced. The focused helper suite stays under `scripts/tests`, is excluded
from normal application collection, and runs with an explicit CPython 3.14 tool
environment.

## Invariants and verification

The report validator must reject malformed JSON and invalid report shapes;
binary absence, version drift, non-zero exit and timeout must never become an
empty successful scan. Locations remain repository-relative POSIX spans, and
unit names are copied only when nose supplies them. Findings have a stable
ordering.

An exception matches a family only when every location matches a key from one
entry. A new unlisted member blocks. `unit` and `members` are exclusive forms;
reasons are non-blank; path matching uses repository-relative full globs; and
named keys never match unnamed fragments. Stale means unmatched in this capped
scan, not proof that duplication disappeared.

The writer must preserve TOML comments and unrelated values, treat reordered
keys for the same entry as an idempotent update, and serialize participating
allow commands with a stable sidecar lock. The lock is advisory and does not
coordinate unrelated editors. Writes retain permissions, sync requested file
and directory state, and remove temporary files on failure.

## Delivery milestones

1. **Pin and isolate the tool.** Add the release digest manifest and installer,
validate the existing production root, and add detector configuration and
contract tests before wiring the scan into lint. Prove a cached correct binary
is a no-op and all discovered binaries report exactly the configured version.
2. **Port the gate contracts.** Add the five support modules and adapted
`scripts/tests` suite. Start with failing focused tests for family matching,
invalid input, report failures, persistence and command construction, then
implement the smallest passing changes. Confirm the application test
interpreter does not collect these helpers.
3. **Integrate Make and CI.** Add install, blocking scan, isolated helper-test
and safe allow commands. Wire lint and standalone scan through provisioning;
cache the binary by OS/architecture and its pins, and cache the actual Python
tool environments using their relevant source/configuration keys.
4. **Adjudicate the selected surface.** Run the initial scan, inspect each
   family and its callers with Leta and codegraph where available, then
   refactor genuine duplication or write a specific exception for intentional
   independent contracts. Repeat until the bounded selected surface is clean.
   Record each decision and its focused tests in this plan and the developer
   documentation.
5. **Prove and deliver.** In a disposable unsaturated workspace, run the four
canary states and compare normalized output across repeated scans. Run the
standalone gate, focused tests, canonical lint, formatter, type checker,
documentation and application-test gates sequentially. Review the complete
diff, commit, push over SSH, establish tracking for
`origin/adopt-nose-code-deduplication`, and open a draft PR without merging.

At each milestone, verify that application dependencies and behaviour remain
unchanged, the scan still selects production sources, the gate fails closed,
and no benchmark or unrelated migration has entered the diff. Stop if a
required platform lacks a trusted prebuilt checksum or if a proposed
duplication fix would require a public API or runtime-dependency change.

## Progress and decision log

Status: implementation complete; final gates passed and delivery is in progress.

- Confirmed the reference commit exists locally in an episodic worktree and
  consulted the specified ADR, merged gate modules and v0.20.0 nose usage,
  configuration, query-JSON and release metadata through Firecrawl.
- Created the requested Leta workspace and renamed the local branch. The
  remote branch does not yet exist; upstream tracking will be set after push.
- Confirmed the package root, excluded first-party areas, Python floor, CI
  operating system and canonical lint target. No reusable atomic persistence
  helper exists.
- Deliberate deviation: use pinned official release archives plus per-platform
  SHA-256 values rather than bootstrapping cargo-binstall. The reference
  revision and this deviation will also be recorded in the final maintainer
  documentation and PR description.
- The first scan exposed that nose exclusions are relative to each configured
  root. The configuration and root-selection contract now use `testing/**`,
  `unittests/**` and `behaviour/**`; a direct JSON query confirmed that only
  direct production modules remain selected.
- The bounded production report selected three families. Independent
  exception constructors retain separate base-class and message contracts;
  router imports and package re-exports retain their distinct dependency roles;
  and the complete-URI and prefix matchers retain distinct suffix semantics
  around their already-shared compiler. Focused tests preserve each contract,
  and all three families have exact-path/name reasoned entries.
- The post-adjudication standalone scan passes with all three families
  explicitly allowed. The final isolated `make duplication-test` passed 153
  tests. Its disposable real-binary fixture covers a clean scan, a blocking
  planted clone, a narrow allow rule, a blocking added member, and
  deterministic normalized output.
- The first full gate run exposed repository spellings, strict Ruff rules in
  the adapted scripts, and one incorrect pin-contract assertion. The prose and
  tests now match repository conventions; lint findings were corrected with
  small validation helpers and reasoned subprocess/download directives. The
  installer rejects non-HTTPS or non-GitHub release URLs, and refuses Linux
  hosts without a known glibc release target. The final sequential run passed
  Make validation, formatting, spelling, duplication tests and scan, lint,
  typechecking, Markdown and Mermaid checks, and application tests (805 passed,
  2 skipped, 19 warnings). `typos.toml` remained unchanged.
- Leta verified router compiler callers. The codegraph workspace was created,
  but its repository re-index timed out; no codegraph result is treated as
  evidence.
