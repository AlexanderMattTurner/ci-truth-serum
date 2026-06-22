- `check-required-reporter` (Tier 2): a gated workflow’s `if: always()` reporter
  jobs must each carry a `# required-check: true|false` classification, so a new
  status check can’t silently ship green-but-never-required. Opt a workflow out
  with `# not-required-check` on its `pull_request:` trigger, or a single reporter
  with `# required-check: false  # <reason>`.
