# Publishing a release

Falcon-Pachinko publishes one source distribution and its wheel to PyPI, then
makes the tag's GitHub Release a digest-checked mirror. The release workflow
uses the same sdist-first build as pull-request CI: plain
`uv build --out-dir dist` builds the sdist, then builds the wheel from that
sdist. The attested files are carried between jobs; publication jobs do not
rebuild them.

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
only when every canonical distribution came from the current attested build. If
the matching bundle is missing, verification fails and leaves the release as a
draft. Recover the original run's `release-dist` artefact and rerun publication
with its distributions and bundle; never attach a bundle for rebuilt bytes. A
retry whose build differs also fails verification even if a bundle is already
attached, because its correspondence cannot be established from the filename.

PyPI is canonical when a filename already exists there. Otherwise an existing
GitHub Release asset is canonical, followed by the current build. Existing
files are never overwritten. A name present only on GitHub is copied to PyPI
from that release asset; a name missing from GitHub is fetched from PyPI. A
digest mismatch fails the run and leaves the release in draft so the conflict
can be investigated without publishing divergent bytes.

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
