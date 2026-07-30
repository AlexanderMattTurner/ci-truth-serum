- `check-versionless-install` (Tier 1 · identity): flags an install command that
  names no version — `pip install ruff`, `apt-get install -y pkg`,
  `pipx install pre-commit`, `uv tool install ruff`, `npm install -g pnpm` — in
  shell scripts and inline workflow `run:` blocks, so CI cannot silently install
  different bytes than the ones you reviewed. Requirements/constraints files,
  local paths and archives, URL/VCS specs, variable-built specs, and local (non
  `-g`) npm installs are out of scope; opt out with `# pin-exempt: <reason>`.
