#!/usr/bin/env bash
# Run cosmic-ray mutation testing over the lint parsers and print the report.
#
# Usage: run-mutation.sh [SESSION_SQLITE]
#
# Drives the four-phase cosmic-ray flow (init -> baseline -> exec -> report) from
# the repo-root cosmic-ray.toml, then prints the human report and the survival
# rate. A non-zero baseline (the unmutated suite must pass) aborts the run loudly.
# The job is diagnostic, not a gate: a surviving mutant is a prompt to add a test,
# not an automatic CI failure, so this script does NOT fail on survivors (the
# report is the artifact). It DOES fail if cosmic-ray itself errors or the
# baseline is red.
set -euo pipefail

session="${1:-cr-session.sqlite}"
config="cosmic-ray.toml"

# A meaningful-but-fast property budget: the full suite runs once per mutant, so
# the CI-grade derandomized 400-example profile would be prohibitively slow here.
export HYPOTHESIS_PROFILE="${HYPOTHESIS_PROFILE:-dev}"

# Fresh session each run so a stale partial DB can't mask new mutants.
rm -f "${session}"

echo "::group::cosmic-ray baseline (unmutated suite must pass)"
# baseline runs the test command with no mutation applied; a non-zero exit means
# the suite is already red and every mutant would falsely read as "killed".
cosmic-ray baseline "${config}"
echo "::endgroup::"

echo "::group::cosmic-ray init"
cosmic-ray init "${config}" "${session}"
echo "::endgroup::"

echo "::group::cosmic-ray exec"
cosmic-ray exec "${config}" "${session}"
echo "::endgroup::"

echo "## Mutation testing report"
cr-report "${session}" --show-output

rate="$(cr-rate "${session}")"
echo "Mutation survival rate: ${rate}"
