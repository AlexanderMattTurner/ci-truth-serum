- All line-oriented shell lints (`check-stderr-suppression`,
  `check-substitution-exit-swallow`, `check-pinned-downloads`,
  `check-secret-file-perms`, plus the joiners `check-exit-suppression`,
  `check-echo-fallback`, `check-stderr-merge-parse` already had privately) now
  scan through ONE shared logical-line joiner, so a construct wrapped across
  physical lines (trailing `|`/`\` continuations, multi-line `$(…)` captures)
  can no longer evade any of them.
- `check-case-default` now finds `case` blocks with tree-sitter-bash: a
  single-line `case … esac` (previously never checked — the hand-rolled stack
  scanner leaked its frame on the same-line `esac`) is analyzed like any other
  block, and a `case` quoted in a string or comment is never mistaken for one.
- `check-flag-arity` parses through the same shared `_bash_ast` grammar module
  as the other AST lints.
