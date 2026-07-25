- `check-failure-notifier-coverage` accepts `--notifier-pattern REGEX`
  (repeatable), teaching notifier DISCOVERY a house sink the built-in patterns do
  not name (`--notifier-pattern tell-the-humans`). As in
  `check-cron-alert-coverage`, the flag extends the built-in patterns rather than
  replacing them, so naming a house sink can never silently un-recognize the
  sinks already matched. The two checks now read one shared sink list
  (`_linecheck.NOTIFIER_PATTERNS`), so a sink taught to one is a sink to both.
