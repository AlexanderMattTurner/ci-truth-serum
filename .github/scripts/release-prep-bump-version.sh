#!/usr/bin/env bash
# Run the base branch's trusted release-prep + changelog assembler on the PR branch.
# Env: BASE_REF, RUNNER_TEMP
set -euo pipefail
: "${BASE_REF:?}"
: "${RUNNER_TEMP:?}"
script=.github/scripts/release-prep.sh
assembler=scripts/assemble-changelog.mjs
retry_lib=bin/lib/retry.bash
git fetch --quiet origin "$BASE_REF"
if git show "FETCH_HEAD:${assembler}" >"${RUNNER_TEMP}/assemble-changelog.mjs" 2>/dev/null; then
  export ASSEMBLE_CHANGELOG="${RUNNER_TEMP}/assemble-changelog.mjs"
else
  echo "::warning::base branch lacks ${assembler}; using the PR's copy (bootstrap only)"
fi
if git show "FETCH_HEAD:${retry_lib}" >"${RUNNER_TEMP}/retry.bash" 2>/dev/null; then
  export RETRY_LIB="${RUNNER_TEMP}/retry.bash"
else
  echo "::warning::base branch lacks ${retry_lib}; using the PR's copy (bootstrap only)"
fi
if git show "FETCH_HEAD:${script}" >"${RUNNER_TEMP}/release-prep.sh" 2>/dev/null; then
  bash "${RUNNER_TEMP}/release-prep.sh"
else
  echo "::warning::base branch lacks ${script}; running the PR's copy (bootstrap only)"
  bash "$script"
fi
