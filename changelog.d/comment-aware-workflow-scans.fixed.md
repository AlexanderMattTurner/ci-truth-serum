- `check-token-fallback` and `check-provenance-repo-url` no longer read a
  YAML `#` comment as workflow content. A comment merely _mentioning_ the
  `secrets.A || secrets.B` idiom or `npm publish` used to produce a finding
  whose only remedy was to pad the workflow (or `package.json`) with
  something it never uses. Comment detection now defers to PyYAML's
  scanner, so a `#` inside a quoted scalar (`title: "#general"`) or a
  `run:` block scalar stays content and a real reference after it on the
  same line is still checked.
