- `release-canary` console script: asserts the semver-max `v*` git tag and the
  changelog's top dated heading agree; prints the labeled values and exits
  non-zero on mismatch. When a `PKGBUILD` is present its `pkgver=` is folded in
  as an optional AUR marker (`--pkgbuild` overrides the path); a build-time
  `pkgver()`/`$(…)` value that can't be read offline is skipped, never a failure.
  Every marker is read locally, so the tool makes no network request.
