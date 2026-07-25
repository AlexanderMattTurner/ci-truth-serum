- New `check-folded-scalar-comment` (Tier 1, honesty): flags a line written as a
  `#` comment inside a folded (`>` / `>-`) YAML block scalar. A folded scalar has
  no comment syntax, so the line is folded into the **value** — and when that
  value is an argument string something shell-splits, the `#` opens a shell
  comment and every argument after it is discarded. The failure shape is the
  worst available: the file documents a boundary that is not in force. Scoped to
  a folded block that also carries a `-`-leading line (an argument string, not
  prose), which is what keeps it quiet on `run:` bodies and Markdown; literal
  (`|`) scalars keep newlines and are deliberately out of scope. Opt out with
  `# allow-folded-scalar-comment: <reason>`.
