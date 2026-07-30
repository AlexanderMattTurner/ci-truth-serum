- The last five lints that decided on text now decide on a grammar. The three
  remaining shell lints — `check-substitution-exit-swallow`,
  `check-stderr-suppression` and `check-secret-file-perms` — read the bash
  grammar (`_bash_ast`), and the two Python lints — `check-global-stdio-swap` and
  `check-toolchain-skips` — read Python's own (`ast`, via the new `_py_ast`; no
  new dependency). Every structural question each was approximating is now
  answered by a node: which stages a pipeline holds, which command a redirect
  belongs to and in what ORDER bash applies it, whether a token is a subcommand
  or a flag's value, which statements follow a create, whether a name is an
  assignment target, and where a call's argument list ends.
- The two Python lints stop flagging their own docstrings, test fixtures and any
  other idiom spelled inside a string literal — 23 findings on this repo's own
  tree, every one a false positive, which is why both had been muted in
  `.pre-commit-config.yaml`. `check-toolchain-skips` is un-muted over the fixture
  files it used to flag; `check-global-stdio-swap` stays skipped for its
  documented scoping reason alone.
- False NEGATIVES fixed: a launch or a `jq`/`yq` producer written after a logging
  call on the same line is now judged (the old scan excused the whole line
  because its first word printed something); a launcher reached through `sudo`,
  an absolute path, or a group redirect (`{ docker build .; } >/dev/null 2>&1`)
  is now seen; a stream bound by `with … as sys.stdout`, across lines, or through
  `setattr(sys, "stdout", …)` is now a swap; and a `skipif` whose `reason=` merely
  mentions `CI` no longer passes as CI-guarded.
- **Behavior change:** `2>&1 >/dev/null` is no longer reported by
  `check-stderr-suppression`. Bash applies redirects left to right, so that order
  dups stderr onto the still-live stdout and then moves only stdout — stderr
  survives, and nothing is suppressed. `>/dev/null 2>&1` still is.
