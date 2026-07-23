- Four new Extras lints, all picked up automatically by the `check-extras` aggregate:
  - `check-drift-guards`: a test whose name/docstring reads as a copies-agree "drift guard" must carry `@pytest.mark.drift_guard("<why no SSOT is feasible>")`, so the missing single source of truth is a reviewed judgement, not an implied one.
  <!-- allow-graceful: this bullet documents the lint's own banned word and opt-out token -->
  - `check-graceful-handwave`: bans "graceful"/"gracefully" in prose (Markdown/RST, every line) and code (comments only) as a stand-in for unstated behaviour; opt out by stating it — `allow-graceful: <what actually happens>`; `--prose` scans a free-standing text file (e.g. a PR body) line-by-line.
  - `check-historical-comments`: bans historical narration in code comments ("renamed from", "now uses", "switched to", …) — describe the current code, or opt out with `# allow-history: <reason>`.
  - `check-doc-line-refs`: bans exact line-number citations of source files in Markdown docs (`<file>.<ext>:<N>`, `(L<N>)`, `~L<N>`, …); fenced code blocks and `CHANGELOG.md` are skipped; suppress a load-bearing one with `<!-- allow-line-ref: <reason> -->`.
