- `check-cron-staleness-optout` is removed. It asked the right question — a cron
  GitHub disabled for repo dormancy produces no failure, so its silence reads as
  health — but it could not answer it: detecting a stopped schedule needs a
  runtime Actions API query, which a pre-commit pack cannot ship. What was left
  was marker bookkeeping plus an existence check on a watchdog workflow, and the
  watchdog is itself a cron, so repo dormancy disables the watcher along with
  everything it watches. The check was structurally blind in the exact case that
  motivated it. A freshness probe on the PR path is the shape that works: it runs
  when a human is already looking, dormancy cannot disable it, and it needs
  neither a marker discipline nor a watchdog registry. `# cron-stale: false`
  markers are no longer read by anything and can be deleted.
