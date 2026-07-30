- `check-versionless-install`: an interpreter name appearing inside the hint text
  being excused no longer counts as executing it. `missing_gate "install it with
'bash setup.bash' (or 'uv tool install pre-commit')"` was flagged because
  `bash` occurred somewhere in the line; the executor is now matched at the
  command word (through `sudo`, `env VAR=…` and an absolute path), so
  `bash -c "pip install x"` still fires and a message that merely names an
  interpreter does not.
