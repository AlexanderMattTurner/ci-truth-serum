- `check-versionless-install`: a redirection in the argument list (`>&2`, `2>&1`,
  `>log`) is no longer read as an unpinned package name, which flagged
  `apt-get install --only-upgrade -y "$pin_spec" >&2` even though its only real
  spec was pinned.
