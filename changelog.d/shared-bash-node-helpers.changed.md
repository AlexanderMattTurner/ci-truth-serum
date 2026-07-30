- Shell lints now read a braced `${VAR}` argument the same way they already read
  `$VAR` and `"$VAR"`. The set of `command` children counted as arguments lived in
  seven separate copies, six of which omitted the `expansion` node type — so in
  those lints the braced spelling alone vanished from the argument list. Two
  verdicts change as a result, both toward agreement with the other two spellings:
  `pip install --target ${D} ruff` is now reported by `check-versionless-install`
  (the flag consumes `${D}`, leaving `ruff` unpinned) and
  `docker ${SUB} build . 2>/dev/null` is no longer reported by
  `check-stderr-suppression` (an opaque word blocks its subcommand scan, as it
  already did for `$SUB`).
