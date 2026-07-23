- `check-pipefail-grep-pipe` now parses scripts with tree-sitter-bash instead of
  scanning physical lines, closing two evasions: a pipeline wrapped across lines
  (trailing `|` / backslash continuations) is analyzed as one pipeline, and a
  `set -o pipefail` appearing only AFTER the pipeline (or in dead code) no longer
  arms — or excuses — the wrong pipes: the first pipefail-enabling command must
  precede the pipeline in source order (function bodies are gated on pipefail
  anywhere in the file, since they run at call time). A `|` inside a string,
  comment, or heredoc is never mistaken for a pipe.
