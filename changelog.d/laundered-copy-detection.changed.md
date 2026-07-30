- `check-drift-guards`: added a third trigger for the LAUNDERED form — a comment
  that claims a value is authoritative (`SSOT`, `canonical`, `single source of
  truth`) while admitting it is a copy (`mirrored from`, `restated here`,
  `hand-maintained`). Neither half fires alone, a denial governing the copy word
  (`read from the SSOT, never a copy`) suppresses, and only comment bodies are
  read — a string literal is a value, not a claim. Closes the gap where a copy
  relabelled as a source of truth used none of the drift vocabulary and so read
  as the sanctioned single-source pattern. Runs on Python tests (a body comment
  inside a `test_*` function) and on JS/TS/shell suites; clear a false positive
  with `drift-guard-ok: <reason>`.
