# Release Workflow

The release workflow builds and publishes a pure Python wheel. Binary wheels
for specific architectures are not produced because the project contains no C
or Rust code.

The process runs automatically when a git tag matching `v*.*.*` is pushed. For
example:

```bash
git tag v1.2.3
git push origin v1.2.3
```

Once triggered, GitHub Actions performs the release steps. The `pure-wheel` job
calls the `pure-python-wheel` composite action, which runs:

```bash
uv build --wheel --out-dir dist
# the wheel artifact is uploaded and attached to the GitHub release
```

To build both artefacts locally, run `make build-release`, which runs
`uv build --out-dir dist`. This builds the source distribution first, then
builds the wheel from it, and writes both files into `dist/`.
