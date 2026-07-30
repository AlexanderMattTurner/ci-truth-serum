- Docs: this package is not published to npm or PyPI — its release _is_ the `v*`
  tag consumers pin with pre-commit's `rev:`. The `sync-required-checks` and
  `release-canary` sections now install from that tag
  (`pip install "git+https://github.com/AlexanderMattTurner/ci-truth-serum@v1.0.0"`)
  instead of a registry name that resolves nowhere, and `package.json` is marked
  `private` so a stray `npm publish` cannot invent a registry copy.
