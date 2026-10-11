# Changelog

## [0.1.0-alpha3] - 2026-10-28

### Changed

- Route WebSocket sessions through a router-owned receive and dispatch loop
  ([#235](https://github.com/leynos/falcon-pachinko/pull/235)).
- Replace distributed socket storage with cluster-aware fan-out
  ([#241](https://github.com/leynos/falcon-pachinko/pull/241)).

### Added

- Publish a source distribution and wheel with provenance through PyPI and
  GitHub Releases
  ([#260](https://github.com/leynos/falcon-pachinko/issues/260)).
- Add a live Falcon ASGI WebSocket server harness for acceptance tests
  ([#236](https://github.com/leynos/falcon-pachinko/pull/236)).
- Provide a stable `ValidationError` import from `falcon_pachinko.utils`
  ([#222](https://github.com/leynos/falcon-pachinko/pull/222)).

### Fixed

- Prevent client-controlled envelope types from invoking lifecycle callbacks
  ([#239](https://github.com/leynos/falcon-pachinko/pull/239)).
- Escape literal URI-template segments when compiling WebSocket routes
  ([#237](https://github.com/leynos/falcon-pachinko/pull/237)).
- Prevent sensitive payload values from leaking into diagnostics
  ([#234](https://github.com/leynos/falcon-pachinko/pull/234)).
- Allow a clean wheel installation to import without `typing_extensions`
  ([#260](https://github.com/leynos/falcon-pachinko/issues/260)).

[0.1.0-alpha3]: https://github.com/leynos/falcon-pachinko/compare/v0.1.0-alpha2...v0.1.0-alpha3
