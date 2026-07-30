- `check-versionless-install`: a project logger's message no longer reads as a
  command. `gb_error "install it: apt install coreutils"`, `log_info`, `note`,
  `_die` and friends are recognized alongside `echo`/`printf`, and a `;`/`&&`
  inside a quoted string no longer starts a new command — so a hint written for
  a human is not flagged, while an install after a real separator
  (`note "x" && apt-get install -y curl`) still is.
