- **Breaking:** `release-canary` no longer reads the npm registry. Its markers
  are the `v*` git tag, the changelog's top dated heading, and an optional
  `PKGBUILD` `pkgver=`, all read locally — the tool now makes no network request
  and needs no registry credentials, so it runs in the same restricted job that
  cut the release. The `--package` flag is gone with the npm marker (no
  `package.json` is read), and so is the `--no-npm` flag added in this same
  release, which existed only to switch the npm marker off.
