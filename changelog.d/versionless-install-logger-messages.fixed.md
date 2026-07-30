- `check-versionless-install`: an install inside a quoted string is only flagged
  when something executes that string (`bash -c`, `sh -c`, `eval`, `ssh`,
  `xargs`). Help text and log messages that name an install for a human —
  `gb_error "install it: apt install coreutils"`,
  `require_command jq "e.g. apt-get install jq"` — no longer fire, without this
  lint having to know every project's logger names. A `;`/`&&` inside a quoted
  string no longer starts a new command either, while an install after a real
  separator (`note "x" && apt-get install -y curl`) still fires.
