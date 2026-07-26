- Removed four checks that policed prose/test style or a bespoke registry
  rather than CI honesty (out of scope per the README's inclusion criteria):
  `check-drift-guards` (a meta-lint on how consumers write their tests),
  `check-historical-comments` (comment-tense prose style),
  `check-graceful-handwave` (bans one word; prose style, no user-visible-bug
  reach), and `check-workflow-secret-names` (required a
  `.github/workflow-secrets.txt` registry file no general consumer has).
  Consumers listing any of these ids must drop the entry when bumping their
  pin; tier-aggregate consumers need no config change.
