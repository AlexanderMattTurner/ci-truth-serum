#!/usr/bin/env bash
# Weekly sensor: name this repo's workflows whose runs failed before any job
# started.
#
# Takes one argument, the `owner/name` of the repo to scan. Publishes the report
# to the run summary and then exits with the scan's own status, so a finding
# turns the run red and a reader sees the table without opening a log.
#
# The report is written BEFORE the exit status is applied. A scan that found
# something exits non-zero, and `set -e` would otherwise end the script with the
# finding still only in a variable.
set -euo pipefail

repo="${1:?usage: startup-failure-scan.sh <owner/name>}"

# `uv run` resolves the console script from pyproject.toml's [project.scripts],
# so this exercises the exact entry point the README tells consumers to install.
report=""
rc=0
report="$(uv run --frozen startup-failure-scan --repo "$repo" --format markdown)" || rc=$?

printf '%s\n' "$report" >>"${GITHUB_STEP_SUMMARY:-/dev/stdout}"
printf '%s\n' "$report"
exit "$rc"
