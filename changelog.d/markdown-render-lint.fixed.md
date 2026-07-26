- README: the “What it checks” tables rendered wrong. Three rows
  (`check-folded-scalar-comment`, `check-gh-slurp-jq`,
  `check-unscoped-tool-grant`) sat one blank line below their table, which ends
  a GFM table — they shipped as stray lines of literal `| pipe | text |`. Two
  more cells carried unescaped `|` characters that split them into phantom
  columns, truncating the visible text and mangling `check-trusted-base`’s
  wording. All are fixed, and markdownlint now runs over every `.md` file (in
  `pnpm lint`, pre-commit, and CI) with a local `orphan-table-row` rule that
  catches and autofixes a severed table row — the one no stock rule sees,
  because the parser never accepted it as a table row to begin with.
