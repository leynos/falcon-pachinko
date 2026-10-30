# ADR-003: Commit uv.lock for development and CI

Status: Accepted

## Context

Issue [#83](https://github.com/leynos/falcon-pachinko/issues/83) recorded the
earlier floating-resolution behaviour: `uv.lock` was ignored so CI resolved the
newest releases permitted by `pyproject.toml` on each run. That exercised newer
compatible releases promptly, but the same change could produce different
development and CI environments over time.

The repository already tracks `uv.lock`, checks that it remains tracked, and
uses Dependabot's root `uv` updater to propose grouped dependency changes.
Those update pull requests can run CI against a reviewed lockfile change.

## Decision

Commit `uv.lock` and require `--locked` for development and CI project
environments. The Developer Guide's
[lock-file policy](developers-guide.md#lock-file-policy) is the authoritative
source for installation, refresh and CI procedures. This record captures the
decision's context and consequences.

## Consequences

Each routine CI run checks one reviewed dependency release set. It does not
automatically test every Falcon release allowed by `>=4,<5`; Dependabot's
grouped `uv` update pull requests provide the review and CI path for newer
releases. Published wheels and source distributions continue to declare the
version ranges from `pyproject.toml`, so consumers resolve dependencies in
their own environments.

The policy makes development and CI environments reproducible. A dependency
declaration change or targeted upgrade must include the reviewed regenerated
lockfile in the same change.
