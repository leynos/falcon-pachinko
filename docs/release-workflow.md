# Release Workflow

The release workflow builds and publishes a pure Python wheel.
Binary wheels for specific architectures are no longer produced.

The process runs automatically when a git tag matching `v*.*.*` is pushed.
It builds the wheel using `uv build`, uploads the artifact, and attaches it
to a GitHub release.
