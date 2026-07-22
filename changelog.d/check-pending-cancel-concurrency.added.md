- `check-pending-cancel-concurrency` (Tier 2): flags a per-ref/per-PR
  `concurrency.group` (workflow-level **or** job-level, which
  `check-static-concurrency` never inspects) on a required-check workflow whose
  `on.pull_request.types` includes a type beyond opened/synchronize/reopened.
  Such types (`labeled`, `closed`, …) fire extra runs on the **same head SHA**;
  GitHub's one-running + one-pending slot per group then cancels a current-SHA
  sibling, and its `always()` reporter resolves `cancelled` → the required check
  goes red with no real failure (`cancel-in-progress` only picks which
  current-SHA run dies). Safe fixes: drop the group or key it on
  `github.run_id`; opt out with `# pending-cancel-ok`. Downstream repos guarding
  this with a file-scoped test (e.g. `test_deps_release_scan_event_gating.py`
  covering only `deps-release.yaml`) can retire it in favor of this lint once
  they bump their pin.
