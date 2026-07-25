- **Breaking:** `check-failure-notifier-coverage` now FAILS CLOSED when no
  notifier workflow can be discovered, where it used to report success. A pass
  from a check that found nothing to check is the vacuous green this package
  exists to catch — and "found nothing" is precisely how a notifier that was
  deleted, renamed, or never synced from the template goes unnoticed. The
  `--require-notifier` flag is replaced (not aliased) by its inverse,
  `--allow-no-notifier`, for a repo that deliberately routes failures nowhere;
  `--notifier FILENAME` (default `ci-failure-notify.yaml`, the shared template's
  spelling) states the filename the fail-closed message should name. The message
  distinguishes "that file is not there" from "that file is there but observes
  nothing" — the second being an inert notifier that reads as coverage in review.
