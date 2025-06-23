# Release Workflow

The release workflow builds and publishes a pure Python wheel.
Binary wheels for specific architectures are no longer produced. The
cross-platform `build` job in `.github/workflows/release.yml` was disabled to
avoid unnecessary maintenance, since the project contains no C or Rust code.

The process runs automatically when a git tag matching `v*.*.*` is pushed. For
example:

```bash
git tag v1.2.3
git push origin v1.2.3
```

Once triggered, GitHub Actions performs the release steps:

```bash
uv build
# the wheel artifact is uploaded and attached to the GitHub release
```
