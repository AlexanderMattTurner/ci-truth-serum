- `check-pending-cancel-concurrency` (Tier 2): flags the two concurrency
  shapes that let GitHub cancel a required check's reporter. (1) A per-ref/
  per-PR `concurrency.group` (workflow-level **or** job-level, which
  `check-static-concurrency` never inspects) on a required-check workflow
  whose `on.pull_request.types` includes a type beyond
  opened/synchronize/reopened. Such types (`labeled`, `closed`, …) fire
  extra runs on the **same head SHA**; GitHub's one-running + one-pending
  slot per group then cancels a current-SHA sibling, and its `always()`
  reporter resolves `cancelled` → the required check goes red with no real
  failure (`cancel-in-progress` only picks which current-SHA run dies).
  Safe fixes: drop the group or key it on `github.run_id`; opt out with
  `# pending-cancel-ok`. (2) A **static** (ref-less) workflow-level group
  with a truthy `cancel-in-progress` on a workflow that declares a required
  check via the `# required-check: true` marker — the residual class the
  static-concurrency lint's decide-gate heuristic misses. A sibling ref then
  cancels the run — and its workflow-level `always()` reporter — wholesale,
  hanging the check at "Expected — Waiting" forever. Per-ref groups and
  non-cancellable groups pass; opt out with
  `# cancellable-required-check-ok`. Downstream repos guarding the type-
  storm case with a file-scoped test (e.g.
  `test_deps_release_scan_event_gating.py` covering only
  `deps-release.yaml`) can retire it in favor of this lint once they bump
  their pin.
