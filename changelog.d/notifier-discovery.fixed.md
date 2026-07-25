- `check-failure-notifier-coverage` no longer goes blind in a repo whose notifier
  workflow is not literally named `ci-failure-notify.yaml`. It matched that one
  basename and nothing else, so a repo calling its notifier anything else
  (`build-publish-notify.yaml`, `alerts.yaml`) got a green from a check that had
  found nothing and verified nothing, and the old `--require-notifier` reported
  the notifier "missing" while it sat in the same directory. That filename is
  still the expected default (it is the shared template's spelling), but it is
  now only the name the fail-closed message states — never a filter. The notifier
  is DISCOVERED: a
  workflow qualifies when it is triggered `on.workflow_run` **and** names a
  notification sink (in its own `name:`, a job's `name:`/`uses:`, or a step's
  `uses:`/`name:`/`run:`). Both halves are required — the trigger alone would
  mistake any CI workflow with a Slack step for the notifier, the sink alone
  would hold an unrelated `workflow_run` consumer to the notifier's
  exhaustiveness invariant. More than one notifier is allowed: coverage is the
  union of their lists, while a stale or duplicated entry is still reported
  against the file that carries it.
- `check-cron-alert-coverage` recognizes an issue-management step as the failure
  sink it is. The pattern was `create[-_]issue`, so a repo that routes failures
  through a house script (`manage-release-failure-issue.sh open`) or a step named
  "Open a tracking issue if the run failed" was reported as routing its failures
  nowhere — a false positive that pushes a repo toward a bogus opt-out marker for
  coverage it actually has. Any issue-management verb (`create`/`open`/`file`/
  `manage`/`report`) preceding `issue`/`issues` now counts; a step that merely
  mentions issues without such a verb (`gh issue list --state open`) still does
  not.
