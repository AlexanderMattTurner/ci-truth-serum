- `check-drift-guards`: added a third trigger for the LAUNDERED form — a comment
  that claims a value is authoritative (`SSOT`, `canonical`, `single source of
truth`) while admitting it is a copy (`mirrored from`, `restated here`,
  `hand-maintained`). Neither half fires alone, and a denial governing the copy
  word (`read from the SSOT, never a copy`) suppresses. Closes the gap where a
  copy relabelled as a source of truth used none of the drift vocabulary and so
  read as the sanctioned single-source pattern. Runs on Python tests and on
  JS/TS/shell suites; clear a false positive with `drift-guard-ok: <reason>`.
- `check-drift-guards`: comments are now located by a grammar rather than a text
  scan — Python's `tokenize` for `.py`, tree-sitter-bash for shell. A `#` inside
  a Python string literal, a shell heredoc body, or a quoted shell word is no
  longer read as a comment. This removes three defects: a lint fixture or error
  message quoting the flagged shape no longer false-positives, a heredoc body
  no longer reads as a run of comments (the probe in
  `.claude/rules/shell-lint-parsing.md`), and — the fail-open — a string literal
  spelling `# not-a-drift-guard:` no longer disarms the structural trigger for
  the whole test. JS/TS still uses the text heuristic; no JS grammar is a
  dependency yet.
