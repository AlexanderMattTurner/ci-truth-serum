- `check-drift-guards` and `check-toolchain-skips` no longer skip the shortest
  test-file names. Each hand-rolled its own "is this a test file?" path filter and
  both were silently DEAD for `test.py`, `x/test.py` and `conftest.py` (and
  drift-guards also for `spec.rb`, `x/spec.js`, `specs/`), because the bare-basename
  alternative demanded a separator after `test`. A `skipif` hiding in `conftest.py`
  — where collection-time skips most naturally live — escaped `check-toolchain-skips`
  entirely. A scope filter's recall bug is invisible: it produces a green vacuous
  pass, never an error. There is now one shared predicate rather than two peers to
  drift, and a guard test asserts nothing else re-derives it.
- `check-claude-model`'s `uses:` matcher put the optional `- ` before the indent
  run (`^-?\s*uses:`), so it matched an indented block-sequence entry only because
  its caller pre-stripped the line. Corrected to `^\s*-?\s*` and the strip dropped,
  so the pattern is right on its own. A commented-out or example `# uses: …` line
  still never matches.
