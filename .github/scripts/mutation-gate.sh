#!/usr/bin/env bash
# Decide whether this mutation run has a new tree to judge, and write run=true or
# run=false to GITHUB_OUTPUT. A scheduled run skips when main still sits at the
# commit the last passing run of this workflow judged; a hand dispatch always runs.
#
# Env: EVENT (github.event_name), HEAD_SHA, GITHUB_REPOSITORY, GITHUB_OUTPUT,
#      GH_TOKEN with actions:read.
set -euo pipefail
: "${EVENT:?}" "${HEAD_SHA:?}" "${GITHUB_REPOSITORY:?}" "${GITHUB_OUTPUT:?}"

run=true
if [[ "${EVENT}" == "schedule" ]]; then
  last="$(gh api "repos/${GITHUB_REPOSITORY}/actions/workflows/mutation.yaml/runs?branch=main&status=success&per_page=1" \
    --jq '.workflow_runs[0].head_sha // ""')"
  if [[ "${last}" == "${HEAD_SHA}" ]]; then
    run=false
    echo "::notice::main is still at ${HEAD_SHA}, which a passing mutation run already judged"
  fi
fi
echo "run=${run}" >>"${GITHUB_OUTPUT}"
