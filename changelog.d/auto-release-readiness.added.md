- Automated release-readiness path adapted from `agent-glovebox`: a daily
  scheduled workflow (`release-readiness.yaml`) asks Claude whether the pending
  `changelog.d/` fragments merit a release and, on a yes, opens a
  `release`-labelled PR that bumps `package.json` and rolls the `CHANGELOG` on an
  `auto-release/vX.Y.Z` branch. A human merges it; `tag-release.yaml` then tags
  `vX.Y.Z`. It never pushes to the default branch (no ruleset-bypass credential —
  just the job's `GITHUB_TOKEN`). Complements the human `release`-label path in
  `release-prep.yaml` and stands down when any `release` PR is already open so the
  two never collide. A self-managed tracking issue
  (`manage-release-failure-issue.sh`) surfaces a broken scheduled run.
