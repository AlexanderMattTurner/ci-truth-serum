- `check-toolchain-skips` no longer skips the shortest test-file names. Its
  hand-rolled "is this a test file?" path filter was silently DEAD for
  `test.py`, `x/test.py` and `conftest.py`, because the bare-basename
  alternative demanded a separator after `test`. A `skipif` hiding in
  `conftest.py` — where collection-time skips most naturally live — escaped
  it entirely. A scope filter's recall bug is invisible: it produces a green
  vacuous pass, never an error. There is now one shared predicate
  (`is_test_path`) and a guard test asserts nothing else re-derives it.
- `check-claude-model`'s `uses:` matcher put the optional `-` before the indent
  run (`^-?\s*uses:`), so it matched an indented block-sequence entry only because
  its caller pre-stripped the line. Corrected to `^\s*-?\s*` and the strip dropped,
  so the pattern is right on its own. A commented-out or example `# uses: …` line
  still never matches.
