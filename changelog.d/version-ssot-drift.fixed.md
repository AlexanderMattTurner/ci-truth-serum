- README `rev:` pins now name `v1.0.0`, the tag that actually exists — the
  documented `v0.2.0` never shipped, so every copy-pasted consumer config failed
  at `pre-commit install-hooks` with an unresolvable rev.
- The pip package version is no longer a stale hand-maintained copy: `pyproject.toml`
  declares `version` as dynamic and reads it from `package.json`, the one file the
  release pipeline bumps, so `pip install ci-truth-serum` reports the released
  version instead of a frozen `0.2.0`.
