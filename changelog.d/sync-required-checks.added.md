- `sync-required-checks` console script (the apply half of
  `check-required-reporter`): derives the required-check set from every job
  carrying `# required-check: true` — expanding each `name:` across its
  `strategy.matrix` — and rewrites the repo's single branch ruleset's
  `required_status_checks` to exactly that set. `--check` reports drift and exits
  non-zero without mutating; the default applies. Reads the marker via the same
  scoping the lint uses, so the two can never disagree.
