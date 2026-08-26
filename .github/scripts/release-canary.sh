#!/usr/bin/env bash
# Post-release verification: assert this repo's version markers agree.
#
# Runs `release-canary` (this package's own console script) on the default
# branch after tag-release.sh has tagged and published, which is the first
# moment the comparison is meaningful: the `v*` tag and the changelog's top
# dated heading only line up once the release is cut, so a pre-merge gate could
# only ever check the state the release is about to leave behind. Verifying here
# catches the half-finished release — a tag push that 403'd, a changelog
# promotion that never ran — on the push that caused it.
#
# The canary reads the `v*` tags from the local repo, and the release job
# checks out shallow (fetch-depth 2, no tags), so fetch them first. A fetch
# that fails is a hard error: without the tag set the canary would read the git
# marker as absent and report a `missing marker` failure that blames the
# release rather than the fetch.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/retry.bash disable=SC1091
source "$SCRIPT_DIR/lib/retry.bash"

if ! retry_cmd 4 2 timeout --kill-after=15 60 git fetch --tags --quiet origin; then
  echo "Error: failed to fetch tags after 4 attempts; the release canary cannot read the git-tag marker" >&2
  exit 1
fi

# `uv run` resolves the console script from pyproject.toml's [project.scripts],
# so this exercises the exact entry point the README tells consumers to install.
# Its exit status is this script's: a mismatch or a missing marker fails the
# job, and the job's failure is what reaches a human.
uv run --frozen release-canary "$@"
