- `check-drift-guards`: added a third trigger for the LAUNDERED form — a comment
  that claims a value is authoritative (`SSOT`, `canonical`, `single source of
  truth`) while admitting it is a copy (`mirrored from`, `restated here`,
  `hand-maintained`). Neither half fires alone, and a denial governing the copy
  word (`read from the SSOT, never a copy`) suppresses. Closes the gap where a
  copy relabelled as a source of truth used none of the drift vocabulary and so
  read as the sanctioned single-source pattern. Runs on Python tests and on
  JS/TS/shell suites; clear a false positive with `drift-guard-ok: <reason>`.
- `check-drift-guards`: the Python pass now locates comments with Python's own
  tokenizer instead of a text scan. A `#` inside a string literal is no longer
  read as a comment, which removes two defects: a lint fixture or error message
  quoting the flagged shape no longer false-positives, and — the fail-open — a
  string literal spelling `# not-a-drift-guard:` no longer disarms the
  structural trigger for the whole test.
