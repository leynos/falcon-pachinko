# Publishing a release

Falcon-Pachinko publishes one source distribution and its wheel to PyPI, then
makes the tag's GitHub Release a digest-checked mirror. The release workflow
uses the same sdist-first build as pull-request CI: plain
`uv build --out-dir dist` builds the sdist, then builds the wheel from that
sdist. The attested files are carried between jobs; publication jobs do not
rebuild them.

`uv build` uses an isolated environment for the backend declared in
`[build-system]`; that backend environment is outside the project's
lock-file policy. Published wheels and source distributions retain the
dependency ranges declared in `pyproject.toml`, so consumers resolve
those ranges in their own environments. See the Developer Guide's
[lock-file policy](developers-guide.md#lock-file-policy).

## Release jobs

A push of a tag matching `v*.*.*` starts these jobs:

| Job               | Work                                                                                                                                              |
| ----------------- | ------------------------------------------------------------------------------------------------------------------------------------------------- |
| `check-version`   | Compare the tag, without its leading `v`, to the exact project version in `pyproject.toml`; classify pre-releases.                                |
| `build-wheels`    | Build the sdist and wheel into the `wheels-pure` artefact.                                                                                        |
| `attest`          | Require one sdist and at least one wheel, attest every distribution, and hand off the files with the tag-named Sigstore bundle as `release-dist`. |
| `draft-release`   | Create or reuse the GitHub Release and snapshot any existing assets for reconciliation.                                                           |
| `publish-pypi`    | Publish only missing filenames through PyPI Trusted Publishing, then wait for PyPI's Simple index to show every expected file.                    |
| `publish-release` | Fill missing draft assets using PyPI's canonical bytes, compare destination digests, then publish the GitHub Release.                             |

An empty or malformed distribution artefact fails before publication. The
GitHub Release remains a draft until PyPI contains the distributions and both
destinations agree on their SHA-256 digests. The Sigstore bundle is attached
only when every canonical distribution came from the current attested build.
Every publication attempt downloads the release's original bundle and canonical
files. It verifies their SHA-256 digests against PyPI, then uses
`gh attestation verify` to verify each file against the bundle, repository,
release workflow identity, tag and source commit. A completed release therefore
passes a rerun even if rebuilt bytes differ. Missing or invalid provenance
fails verification and keeps a draft unpublished.

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
from PyPI. A digest mismatch fails the run and leaves the release in draft so
the conflict can be investigated without publishing divergent bytes.

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

The project currently builds with `setuptools>=61.0` and `wheel`. Constraining
and hash-binding build requirements, and deciding how `uv.lock` is maintained,
remain tracked by
[issue #230](https://github.com/leynos/falcon-pachinko/issues/230) and
[issue #209](https://github.com/leynos/falcon-pachinko/issues/209). The release
workflow limits the build job to read access and passes its attested artefact
to publication jobs, so those jobs do not rebuild with publication credentials.

The release process and the GitHub Release's role as a reconciled mirror are
recorded in [ADR-002](adr-002-attested-reconciled-release-pipeline.md).
