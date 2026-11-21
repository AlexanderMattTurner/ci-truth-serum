#!/usr/bin/env bash
# Decide whether this mutation run has a new tree to judge, and write run=true or
# run=false and the run's scope to GITHUB_OUTPUT. A scheduled run skips when a
# passing run of this workflow already judged HEAD at this scope or wider: a full
# run covers a daily one, and a daily run does not cover a full one. A hand
# dispatch always runs.
#
# The scope is read from each run's name, which the workflow's `run-name` ends
# with `(daily)` or `(full)`.
#
# Env: EVENT (github.event_name), HEAD_SHA, SCOPE (daily or full),
#      GITHUB_REPOSITORY, GITHUB_OUTPUT, GH_TOKEN with actions:read.
set -euo pipefail
: "${EVENT:?}" "${HEAD_SHA:?}" "${SCOPE:?}" "${GITHUB_REPOSITORY:?}" "${GITHUB_OUTPUT:?}"

case "${SCOPE}" in
full) covering='\(full\)$' ;;
daily) covering='\((daily|full)\)$' ;;
*)
  echo "::error::SCOPE must be daily or full, got '${SCOPE}'" >&2
  exit 1
  ;;
esac

run=true
if [[ "${EVENT}" == "schedule" ]]; then
  # The passing runs at HEAD, by name. One page holds every run one commit has had.
  judged="$(gh api "repos/${GITHUB_REPOSITORY}/actions/workflows/mutation.yaml/runs?branch=main&status=success&head_sha=${HEAD_SHA}&per_page=100" \
    --jq '.workflow_runs[].display_title')"
  if grep -qE "${covering}" <<<"${judged}"; then
    run=false
    echo "::notice::a passing mutation run already judged ${HEAD_SHA} at ${SCOPE} scope or wider"
  fi
fi
echo "run=${run}" >>"${GITHUB_OUTPUT}"
echo "scope=${SCOPE}" >>"${GITHUB_OUTPUT}"
