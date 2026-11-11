# Publishing a release

Falcon-Pachinko publishes one source distribution and its wheel to PyPI, then
makes the tag's GitHub Release a digest-checked mirror. The release workflow
uses the same sdist-first build as pull-request CI: `make release-wheel` builds
the sdist, then builds the wheel from that sdist twice and compares the two
SHA-256 digests. The attested files are carried between jobs; publication jobs
do not rebuild them.

`uv build` uses an isolated environment for the backend declared in
`[build-system]`; that backend environment is outside the project's lock-file
policy, so its inputs are pinned and hash-bound separately. Published wheels
and source distributions retain the dependency ranges declared in
`pyproject.toml`, so consumers resolve those ranges in their own environments.
See the Developer Guide's
[lock-file policy](developers-guide.md#lock-file-policy).

## Release jobs

A push of a tag matching `v*.*.*` starts these jobs:

| Job               | Work                                                                                                                                                         |
| ----------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `check-version`   | Compare the tag, without its leading `v`, to the exact project version in `pyproject.toml`; classify pre-releases.                                           |
| `build-wheels`    | Build the sdist and wheel into the `wheels-pure` artefact.                                                                                                   |
| `attest`          | Require one sdist and at least one wheel, attest every distribution, and hand off the files with the tag-named Sigstore bundle as `release-dist`.            |
| `draft-release`   | Create or reuse the GitHub Release and snapshot any existing assets for reconciliation.                                                                      |
| `publish-pypi`    | Publish only missing filenames through PyPI Trusted Publishing, then wait for PyPI's Simple index to show every expected file.                               |
| `publish-release` | Fill missing draft assets using PyPI's canonical bytes, verify their provenance before upload, compare destination digests, then publish the GitHub Release. |

An empty or malformed distribution artefact fails before publication. The
GitHub Release remains a draft until PyPI contains the distributions and both
destinations agree on their SHA-256 digests. The Sigstore bundle is attached
only when every canonical distribution came from the current attested build. A
staged file whose bytes differ from the current build must verify against the
release's original bundle before it is uploaded: the run downloads that bundle
and uses `gh attestation verify` with the repository, release workflow
identity, tag and source commit. Bytes identical to the current build are
already covered by the build job's measurement. After the upload, both
destinations are re-checked and the release's files are verified the same way.
A completed release therefore passes a rerun even if rebuilt bytes differ.
Missing or invalid provenance fails verification before any asset is attached
and keeps a draft unpublished.

To recover an interrupted publication, identify the original workflow run that
built the bytes PyPI holds and rerun only its failed jobs:

```sh
gh run rerun ORIGINAL_RUN_ID --failed --repo leynos/falcon-pachinko
```

Keep its successful build and attestation jobs intact: the failed publication
jobs consume that run's original `release-dist` artefact. GitHub permits reruns
for 30 days, and the artefact must still be retained. A full rerun rebuilds the
distributions; downloading an old handoff locally does not make a new run use
it. If the original run cannot be rerun or its artefact has expired, this
workflow has no saved-handoff recovery input. Keep the draft unpublished and
investigate recovery before attempting publication. Never attach a rebuild's
incompatible bundle.

PyPI is canonical when a filename already exists there. Otherwise an existing
GitHub Release asset is canonical, followed by the current build. Existing
files are never overwritten. A name present only on GitHub is copied to PyPI
after its original bundle verifies the downloaded bytes and binds their exact
filename and digest. All carried files are validated before staging changes;
missing or invalid provenance stops publication. Identical bytes from the
current attested build need no carry. A name missing from GitHub is fetched
from PyPI, and a fetched file is uploaded only when its bytes equal the current
build or its original attestation verifies. A digest mismatch fails the run and
leaves the release in draft so the conflict can be investigated without
publishing divergent bytes.

The jobs use per-job permissions. Version checking and distribution building
receive `contents: read`; attestation also receives `id-token: write` and
`attestations: write`; drafting and final GitHub publication receive
`contents: write`; PyPI publication receives only `contents: read` and
`id-token: write`. Checkout credentials are not persisted. Release concurrency
is keyed by the tag and does not cancel an in-progress run. Workflow actions
are pinned to reviewed full commit SHAs.

Re-running a tag is safe. The workflow reuses its release, skips filenames
already present on PyPI, and uploads only assets missing from a draft. It does
not upload to an already-published release. If a run fails on a digest
mismatch, keep the release as a draft, compare the PyPI and GitHub asset
hashes, correct the conflicting destination through its normal maintenance
process, and rerun the same tag. Do not delete or replace an asset merely to
make the workflow pass.

## Preparing the next version

The first release using this pipeline is `0.1.0-alpha3`. Prepare it before
creating the tag:

1. Set `project.version` in `pyproject.toml` to exactly `0.1.0-alpha3`, keeping
   the alpha classifier.
2. Add the release entry to `CHANGELOG.md` using the repository's Common
   Changelog format.
3. Run `uv lock` and commit the resulting `uv.lock` update. The lock file uses
   the normalized PEP 440 version `0.1.0a3`; the release check compares the
   literal project version to the tag with only its leading `v` removed.
4. Run the distribution and package-content checks, then merge the version and
   changelog update before tagging.
5. Create and push the exact tag:

   ```sh
   git tag v0.1.0-alpha3
   git push origin v0.1.0-alpha3
   ```

The version check intentionally rejects alternate spellings, including a tag
whose version differs from `pyproject.toml` after the leading `v` is removed.
For later releases, update the manifest, lock file, and changelog together
before pushing the corresponding `v*.*.*` tag.

## One-time publishing settings

Before the first tag, register the `falcon-pachinko` project name on PyPI. If
the name is unavailable, choose a different distribution name and update the
installation instructions while retaining the `falcon_pachinko` import package.
Configure a PyPI Trusted Publisher for owner `leynos`, repository
`falcon-pachinko`, workflow `release.yml`, and GitHub environment `pypi`. A
pending publisher may be registered before the first upload; it becomes the
publisher for the project after the first successful upload.

In GitHub repository settings, create the `pypi` environment and restrict its
deployment branches and tags to `v*.*.*` tags. PyPI's publisher binding must
name this same environment. No long-lived PyPI API token is used or stored.

PyPI's Trusted Publishing guide recommends its publishing action and shows a
GitHub-hosted `ubuntu-latest` runner. This release workflow uses the
repository's Ubicloud runner for all six release jobs, including PyPI
publication. Keep that runner choice deliberate and verify that the runner's
OIDC identity remains accepted by PyPI when changing the workflow or its
environment settings.

## Build requirements and credentials

The release build runs the PEP 517 backend declared in `[build-system]` in an
isolated environment. That environment never reads `uv.lock`, so the build
inputs are constrained separately:

- `pyproject.toml` pins each backend requirement to one exact release. A range
  or wildcard pin is rejected by `tests/test_release_build_inputs.py`, because
  it could select an unreviewed future release.
- `build-constraints.txt` is generated from those pins with
  `uv pip compile --generate-hashes --universal --python-version 3.12`. It pins
  every transitive build requirement and records the SHA-256 digest of each
  distribution.
- The build recipes pass `--build-constraint build-constraints.txt`,
  `--require-build-hashes` and `--no-cache`. uv refuses a requirement outside
  the constraints file, a distribution whose digest is not listed, and a cached
  wheel that would bypass the check.
- Builds stay isolated. `--no-build-isolation` is never used: it would run the
  backend from the project environment, which is a different, unreviewed set of
  packages.

`--require-build-hashes` is a preview option, added in uv 0.13.0. The release
workflow and the composite build action pin uv 0.13.2, so they have it; a local
`uv` older than 0.13.0 rejects the flag, and `make release-wheel` then fails
with an unknown-argument error rather than building.

CI also proves the flag is enforced, not merely passed. It runs the real
isolated sdist build once with the committed constraints as a control, then
with every permitted hash for `wheel` replaced, and requires the second build
to fail with a hash mismatch and write no distribution. That test is opt-in
(`RELEASE_BUILD_HASH_TEST=1`) because `--no-cache` downloads the
backend again; the workflow's release-build security step sets it, and a local
`make test` skips it.

`SOURCE_DATE_EPOCH` is taken from `git log -1 --format=%ct`, so the
distributions carry the commit timestamp rather than the time of the build.

`make release-wheel` runs `scripts/release_wheel.py`, which builds the sdist
once, then builds the wheel from that sdist twice into separate directories and
compares the two SHA-256 digests. A difference fails the build. Both builds use
the same runner, Python minor version and uv release, so the check proves the
recipe is deterministic within one environment; it does not prove
cross-environment reproducibility. CI runs the same target on every pull
request, and unit tests drive each failure branch of the comparison with
fabricated build outputs. The wheel is pure Python, so the release does not use
cibuildwheel and has no second build path to constrain.

### Updating the build backend

1. Change the exact pins in `[build-system].requires` in `pyproject.toml`.
2. Run `make update-build-constraints` to regenerate `build-constraints.txt`.
3. Review the version and hash diff, and the upstream release notes for the new
   backend release.
4. Run `make release-wheel` to confirm the two builds still agree.
5. Merge through a pull request.

A Dependabot update to `[build-system].requires` that does not regenerate the
constraints file fails `tests/test_release_build_inputs.py`, because the pins
no longer match.

### Credentials

The `build-wheels` job holds `contents: read` only, and every checkout in the
release and CI workflows sets `persist-credentials: false`, including the
composite action. The build job records the wheel and sdist names and SHA-256
digests as job outputs. The `attest` job downloads the `wheels-pure` artefact
and runs `scripts/verify_build_distributions.py` against those outputs before
it attests anything. `publish-pypi` and `publish-release` consume the
`release-dist` artefact that attestation produced and run the same verification
before they upload or publish. Only `draft-release` and `publish-release` hold
`contents: write`, and neither runs a build command.

The committed `uv.lock` covers development and CI project environments, not the
isolated build backend; see the Developer Guide's
[lock-file policy](developers-guide.md#lock-file-policy). Issue
[#209](https://github.com/leynos/falcon-pachinko/issues/209) covers runtime,
development and extra dependencies, not isolated build requirements.

The release process and the GitHub Release's role as a reconciled mirror are
recorded in [ADR-002](adr-002-attested-reconciled-release-pipeline.md).
