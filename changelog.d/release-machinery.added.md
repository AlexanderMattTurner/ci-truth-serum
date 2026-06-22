- Release machinery ported from `claude-guard`: per-change fragments under
  `changelog.d/`, assembled into `CHANGELOG.md` by `scripts/assemble-changelog.mjs`.
  Labeling a PR `release` triggers a conservative (patch/minor, never major)
  version bump on the PR branch (`release-prep.yaml`); merging it tags `vX.Y.Z`
  and publishes the GitHub Release with that version’s notes (`tag-release.yaml`).
