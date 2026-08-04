- The three Claude reviewer steps now grant `Read`/`Edit` with paths instead of
  bare whole-tool names. A bare `Read` or `Write` is applied after the per-path
  check and overrides it, so the `--add-dir` beside it confined nothing: the
  reviewer could read any path on the runner (including the artifact token) and
  overwrite any file in the checkout. Each step now reads the workspace and the
  sanitized input directory, and writes only its own output file.
- The phone-home secret scan verifies the gitleaks download against a pinned
  checksum again, instead of piping an unverified release tarball into `tar`.
