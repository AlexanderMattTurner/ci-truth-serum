- `check-failure-notifier-coverage` and `check-cron-alert-coverage` now answer
  "is this workflow's failure routed to a human?" from ONE shared predicate,
  instead of computing overlapping and mutually inconsistent obligations. A
  failure is routed when the workflow self-notifies on the failure path, when a
  discovered notifier lists it in `on.workflow_run.workflows`, when a
  `pull_request` trigger puts its red on a PR, or when it carries a reasoned
  `# cron-alert: false` marker.
- **Behaviour change:** `check-failure-notifier-coverage` no longer demands that
  the notifier list EVERY push/schedule workflow — only the residual whose
  failures reach nobody else. Forcing in an opted-out workflow silently undid the
  sibling lint's sanctioned marker, a self-notifying workflow got paged twice, and
  a workflow whose failure is already a check on the PR paged for a red a reviewer
  was looking at. A repo may still watch more than that minimum: a listed name is
  reported stale only when it matches no workflow in the tree.
- **Behaviour change:** `check-cron-alert-coverage --require-alert` now accepts a
  failure notifier that watches the scheduled workflow as coverage, so a repo whose
  crons are covered centrally is no longer told to add a notification step that
  would page twice. A `pull_request` trigger is deliberately NOT accepted there: a
  cron fire has no pull request. The `# cron-alert: false` marker is also read from
  the `push:` key, so a push-only workflow can state the same decision.
