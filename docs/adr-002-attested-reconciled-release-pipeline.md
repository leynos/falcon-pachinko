# ADR-002: Attested, reconciled release pipeline

Status: Accepted

## Context

The existing wheel-only release workflow published a GitHub Release before
attaching its asset, did not publish to PyPI, and built distributions directly
from the source tree. Its broad write permission, mutable action references,
persisted checkout credentials, and cancellation of in-progress runs left
avoidable risks in a privileged workflow. The artefact-download layout also
made it possible for an empty release to be published without a clear failure.

Falcon-Pachinko is pure Python. Its distribution name is `falcon-pachinko` and
its import package is `falcon_pachinko`. Setuptools discovers
`falcon_pachinko*`, excluding the internal `falcon_pachinko.behaviour` and
`falcon_pachinko.unittests` test trees. The public `falcon_pachinko.testing`
package is part of the distribution. Building the wheel from the sdist and
inspecting both archives verifies those packaging boundaries rather than
assuming the source tree and archive contents match.

The release also needs two destinations with distinct roles. PyPI provides the
consumer installation index and its Simple API; GitHub Releases provide a
repository-visible copy and generated release notes. A tag may be retried after
partial publication, so those destinations must converge on one set of bytes
without overwriting immutable files or publishing an incomplete release.

## Decision

### Build and validation

Build both distributions with plain `uv build --out-dir dist`. `uv` builds the
sdist first and builds the wheel from that sdist, making sdist completeness a
prerequisite for every wheel. The same composite build action runs in pull
request CI and on release tags. CI requires exactly one sdist and one wheel;
attestation collects every wheel with exactly one sdist and rejects unexpected
files. Archive checks verify the import modules, public testing package,
package data, wheel metadata, and installed imports.

Keep the pure-Python build path and remove the unused cibuildwheel matrix and
its local action. Do not add native-platform builds for a package with no
native code.

### Versioning and release graph

Require the tag after removing its leading `v` to match the `project.version`
string in `pyproject.toml` exactly. Classify pre-releases from that version and
pass the result to the GitHub Release publication step. For the first release
through this process, prepare version `0.1.0-alpha3`, update `uv.lock` with
`uv lock` (which records the normalized version `0.1.0a3`), and add the
matching Common Changelog entry before tagging.

Use six jobs: `check-version` and `build-wheels` run in parallel; `attest`
needs both; `draft-release` needs the version check and attestation;
`publish-pypi` needs attestation and the draft; `publish-release` needs the
version check, draft, and successful PyPI publication. Every job that attests,
uploads, or releases is gated by the version check. Give each job a 30-minute
timeout and retain the Ubicloud runner label.

Attest the one sdist and every wheel produced by the build. Put distributions
and the tag-named Sigstore bundle into one `release-dist` artefact. Later jobs
consume those bytes; publication jobs do not rebuild. The tag-only bundle
filename makes retries idempotent. Attach that bundle to GitHub only when all
canonical distribution bytes match the current attested build. An existing
published file with different bytes requires the earlier build's bundle. Verify
that bundle cryptographically against the downloaded canonical files, the
repository's release workflow identity, tag and source commit. If it is absent
or invalid, verification fails and the draft remains unpublished. Recover by
rerunning the original run's failed publication jobs while its `release-dist`
artefact remains available.

### Destinations and retry behaviour

Publish to PyPI first through `pypa/gh-action-pypi-publish` and Trusted
Publishing. Read the PEP 691 Simple index with bounded retries and a request
timeout; an index 404 means the project has no published files yet, while other
HTTP errors fail. Stage only filenames PyPI lacks, publish with
`skip-existing`, and succeed without invoking the upload action when there is
nothing to publish. Wait for the index to list all expected filenames before
continuing.

Create or reuse a GitHub Release as a draft. Publish the release only after
PyPI holds all expected distributions. For each filename, choose canonical
bytes in this order: PyPI, an existing GitHub Release asset, then this run's
attested build. Fill missing destination names only. A GitHub-only file is
copied into PyPI; a GitHub asset missing for a PyPI name is fetched from PyPI.
Validate fetched hashes against the Simple index and compare final SHA-256
digests at both destinations. Never overwrite or clobber an existing GitHub
asset. Fail on an empty artefact set. Leave a draft in place on any mismatch;
remove draft status only after successful convergence, setting the GitHub
pre-release flag from the checked project version.

Queue repeated runs for the same tag by setting `cancel-in-progress: false`.
Reuse existing releases, skip already published PyPI filenames, and upload only
missing assets to a draft. A rerun of an already-published release uploads no
GitHub assets. Reconciliation, rather than rebuild-and-replace, makes
interrupted runs safe to retry.

### Privileges and actions

Set workflow-level permissions to `{}` and grant only per-job scopes:

- `check-version` and `build-wheels`: `contents: read`.
- `attest`: `contents: read`, `id-token: write`, and `attestations: write`.
- `draft-release` and `publish-release`: `contents: write`.
- `publish-pypi`: `contents: read` and `id-token: write` only.

Set `persist-credentials: false` on every checkout. Pass workflow context
values into shell scripts through quoted environment variables; shell steps use
`set -euo pipefail` and do not interpolate `${{ ... }}` into `run:` text. Pin
every third-party action in the release workflow and build composite action to
a reviewed full commit SHA. Do not use `softprops/action-gh-release`, silent
`find`/`xargs` uploads, stored PyPI tokens, or release telemetry. The separate
credential constraints and hash-bound backend requirements remain in
[issue #230](https://github.com/leynos/falcon-pachinko/issues/230), pending the
`uv.lock` decision in issue #209. Immutable action pin coverage for the other
workflows remains in issue #233.

Use the GitHub `pypi` environment, restricted to `v*.*.*` tags, and bind the
PyPI Trusted Publisher to owner `leynos`, repository `falcon-pachinko`, workflow
`release.yml`, and environment `pypi`. Register the project name and publisher
before the first release; a pending publisher is acceptable until that upload.
The PyPA Trusted Publishing guide recommends its publishing action and presents
a GitHub-hosted Ubuntu runner. This repository keeps the requested workflow on
Ubicloud; preserve that choice explicitly and verify OIDC publishing when
runner configuration changes.

## Consequences

Every release is built from a validated sdist and produces an attested set of
files that can be traced to the workflow run. Pull requests detect a missing or
duplicate sdist before a release is tagged. Publication uses short-lived OIDC
credentials and a read-only build job, while release write access is limited to
the jobs that update GitHub Releases.

PyPI is the canonical destination for names it already contains, and the GitHub
Release converges to those same bytes. Retries can complete a partial
publication without clobbering assets. Digest disagreement leaves a visible
draft for investigation instead of publishing inconsistent files. Existing
artefacts may have been built by earlier runs; the current run's bundle is
attached only when it truthfully attests every canonical distribution.

The release process relies on maintainers keeping `pyproject.toml`, `uv.lock`,
`CHANGELOG.md`, and the tag in sync. The PyPI project and Trusted Publisher,
and the GitHub environment restriction, are one-time settings outside the
repository and must be checked before the first tag. The chosen Ubicloud
publisher runner differs from the GitHub-hosted runner shown in PyPA guidance,
so its OIDC compatibility remains an operational assumption to verify.

The exact build-backend constraints and hash binding are deliberately deferred
to issue #230, which depends on the repository's lock-file decision in
[issue #209](https://github.com/leynos/falcon-pachinko/issues/209). This ADR
records the artefact handoff and privilege separation that let publication
consume attested bytes without resolving build requirements again.
