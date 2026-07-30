#!/bin/bash
# One-command setup for the Claude automation template

set -euo pipefail

echo "Setting up Claude automation template..."

# Configure git hooks
git config core.hooksPath .hooks

if [[ -f package.json ]]; then
  # Install pnpm if not available, at the version package.json's `packageManager`
  # field already declares — reading that field is what keeps the bootstrap off
  # "whatever npm serves today" without a second copy of the version to drift.
  if ! command -v pnpm &>/dev/null; then
    pnpm_spec="$(node -p "require('./package.json').packageManager || 'pnpm'")"
    echo "Installing ${pnpm_spec}..."
    npm install -g "${pnpm_spec}"
  fi

  # Install dependencies (postinstall also sets core.hooksPath, redundantly)
  pnpm install
fi

# Install Python dependencies if applicable
if [[ -f uv.lock ]] && command -v uv &>/dev/null; then
  uv sync
fi

# Verify setup
if [[ "$(git config core.hooksPath)" = ".hooks" ]]; then
  echo ""
  echo "✓ Setup complete!"
  echo ""
  echo "Next steps:"
  echo "  1. Edit CLAUDE.md with your project details"
  if [[ -f package.json ]]; then
    echo "  2. Configure scripts in package.json"
  fi
  echo "  Start coding!"
else
  echo ""
  echo "⚠ Warning: Git hooks may not be configured correctly."
  echo "  Run: git config core.hooksPath .hooks"
fi
