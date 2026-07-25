#!/usr/bin/env bash
# Run the base branch's trusted release-prep script plus the libraries it shells
# out to on the PR branch.
# Env: BASE_REF, RUNNER_TEMP
set -euo pipefail
: "${BASE_REF:?}"
: "${RUNNER_TEMP:?}"
script=.github/scripts/release-prep.sh

# stage_trusted REPO_PATH ENV_VAR — copy the base branch's REPO_PATH into
# $RUNNER_TEMP and point ENV_VAR at it, so this privileged job runs the trusted
# copy instead of the PR-controlled checkout. When the base branch has no copy —
# the bootstrap case for the very PR that adds the file — ENV_VAR is left unset
# and release-prep.sh falls back to its in-tree path.
stage_trusted() {
  local repo_path="$1" env_var="$2" staged
  staged="${RUNNER_TEMP}/${repo_path##*/}"
  if ! git show "FETCH_HEAD:${repo_path}" >"$staged" 2>/dev/null; then
    echo "::warning::base branch lacks ${repo_path}; using the PR's copy (bootstrap only)"
    return 0
  fi
  export "${env_var}=${staged}"
}

git fetch --quiet origin "$BASE_REF"
stage_trusted scripts/assemble-changelog.mjs ASSEMBLE_CHANGELOG
stage_trusted bin/lib/retry.bash RETRY_LIB
stage_trusted bin/lib/release-model-call.bash MODEL_CALL_LIB

if git show "FETCH_HEAD:${script}" >"${RUNNER_TEMP}/release-prep.sh" 2>/dev/null; then
  bash "${RUNNER_TEMP}/release-prep.sh"
else
  echo "::warning::base branch lacks ${script}; running the PR's copy (bootstrap only)"
  bash "$script"
fi
