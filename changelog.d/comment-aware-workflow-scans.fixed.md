- `check-workflow-secret-names`, `check-token-fallback` and
  `check-provenance-repo-url` no longer read a YAML `#` comment as workflow
  content. A comment merely _mentioning_ `secrets.NAME`, the
  `secrets.A || secrets.B` idiom, or `npm publish` used to produce a finding
  whose only remedy was to pad `.github/workflow-secrets.txt` (or
  `package.json`) with something the workflows never use — weakening the very
  allowlist that catches a misspelled secret. Comment detection now defers to
  PyYAML's scanner, so a `#` inside a quoted scalar (`title: "#general"`) or a
  `run:` block scalar stays content and a real reference after it on the same
  line is still checked.
