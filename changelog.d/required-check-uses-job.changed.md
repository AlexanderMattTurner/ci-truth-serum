- `check-required-reporter`: also rejects a `# required-check: true` marker on
  a job that calls a reusable workflow (`uses:`). GitHub reports that job's
  check run as `<caller job name> / <called job name>`, never the job's own
  `name:` that `sync-required-checks` registers, so the ruleset would require
  a context nothing reports. Put the marker on a caller-local reporter job
  that `needs:` the call instead.
