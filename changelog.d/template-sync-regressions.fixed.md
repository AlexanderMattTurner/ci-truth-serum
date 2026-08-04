- Restored the repo-local behaviour that the template sync overwrote: the ntfy
  notifier again writes a `::warning::` annotation on each path that notifies
  nobody, the phone-home workflow again reads one `$PHONE_HOME_DIR` and installs
  gitleaks through the checksum-verifying installer, and `release-canary.sh` again
  runs this package's own `release-canary` console script.
- `decide-pr-review-trigger.sh` no longer passes `--slurp` and `--jq` to one
  `gh api` call. `gh` rejects that pair while parsing its arguments, so the call
  exited 1 before any request and the `|| true` swallowed it — every push read as
  "no reviewer hold" and the automatic re-review never fired.
