- `check-versionless-install` now analyses the real bash grammar (tree-sitter)
  instead of scanning text, which removes a class of false positive rather than
  another instance of it: a redirection (`>&2`) is a `file_redirect` and never a
  package spec, a quoted message is a `string` argument of the command that
  prints it, a heredoc body is a heredoc, `&&`/`;`/`|` genuinely separate
  commands, and a `$VAR` spec is an expansion node. An install inside a string is
  judged only where something executes that string (`bash -c`, `eval`, `ssh`,
  `xargs … sh -c`), which is parsed as its own script.
- **Behavior change:** a finding is reported at the line of the install command
  itself, not at the start of the backslash-joined block containing it — so
  `apt-get update && \` on one line and `apt-get install …` on the next is
  reported at the install. A `# pin-exempt:` annotation must therefore sit on (or
  directly above) the install command, which is where it belongs.
