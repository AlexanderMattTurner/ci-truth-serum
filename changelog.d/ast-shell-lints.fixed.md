- Every shell lint that decided on text now decides on the bash grammar:
  `check-exit-suppression`, `check-echo-fallback`, `check-stderr-merge-parse`,
  `check-pinned-downloads` and `check-gh-slurp-jq`. Each stops reporting its own
  banned idiom when it appears in a printed message or a heredoc body — text no
  shell executes — and each removes further structural false positives listed in
  its commit: a quoted `;` splitting a command that was never split, a redirect
  belonging to a different command, a URL path segment read as a command name, a
  redirection read as a flag. Two false NEGATIVES are fixed too: a redirect
  spelled inside a string counted as a real output discard, and a git mutation
  named inside a string revoked an allowance it should not have.
- `check-gh-slurp-jq` now scans inline workflow `run:` blocks. `run_tier` already
  classified it `SHELL_OR_WORKFLOW_YAML`, but its `main()` read a workflow YAML as
  though it were one shell file, so the routing was declared and never ran.
- **Behavior change:** `echo x || true` is now reported by
  `check-exit-suppression`. Its previous excuse list for printing commands was the
  false-positive mechanism, and a suppression on a printing command is still a
  suppression; annotate with `# allow-exit-suppress: <reason>` where it is
  deliberate.
