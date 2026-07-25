- Every opt-out annotation now requires its token to stand ALONE, on both ends.
  The shared matcher delimited the token with `\b`, which matches at a word/`-`
  transition — and nearly every token in this package is hyphenated. So one
  hook's opt-out was satisfiable by a DIFFERENT annotation whose slug merely
  contained it: `# really-allow-unbounded: …` suppressed a hook asking for
  `allow-unbounded`, and (in the reason-free form) `# pin-comment-ok-ish`
  suppressed one asking for `pin-comment-ok`. A neighbouring lint's marker could
  silently disarm another lint — a fail-open. Both edges are now stated as
  zero-width lookarounds over the characters a token is spelled from.
- `opted_out` (the concurrency lints' whole-file comment scan) was a bare
  substring test, open at both ends: `# no-static-concurrency-ok-here` — or prose
  merely mentioning the token — suppressed the check. It now routes through the
  same shared matcher, so it inherits the stand-alone-token guarantee.
- `check-frozen-head-sha`'s `# frozen-head-ok:` opt-out no longer accepts a
  reason borrowed from the NEXT line. It hand-spelled the reason gap as `\s*`,
  which crosses a newline, and it searches a whole multi-line job block — so a
  bare `# frozen-head-ok:` ending a line suppressed the lint with an empty claim.
