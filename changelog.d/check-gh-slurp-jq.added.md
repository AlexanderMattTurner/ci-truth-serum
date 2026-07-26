- New `check-gh-slurp-jq` (Tier 1, honesty): flags a `gh api` call `gh` refuses
  to run — `--slurp` alongside `--jq`/`--template`, or `--slurp` with no
  `--paginate`. Both are rejected while `gh` parses its arguments, before any
  request goes out (verified against gh 2.86.0, with valid-flag controls proving
  the rejection precedes the HTTP request), so the call exits non-zero on _every_
  run: it has never worked. That is worse than a loud bug — a site that swallows
  the failure reads as a permanent green, and one that does not reds a scheduled
  job 100% of the time. Backslash-continued calls are read whole, so a `--jq` on
  a later line still counts, and flag matching is confined to the pipeline
  segment the `gh api` token opens so a downstream `| jq -r …` is never mistaken
  for one of its flags. Opt out with `# allow-gh-slurp-jq: <reason>`.
