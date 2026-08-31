#!/usr/bin/env bash
# Resolves a PR's merge conflicts: install the pinned Claude CLI from the trusted
# base worktree, then walk the configured credentials, running the per-file
# fan-out (auto-resolve/fanout.sh) under each until one produces a resolution.
#
# A rung advances only when the one before it produced NO usable run at all —
# no execution log, or a log reporting is_error. A run that reached the model and
# judged a conflict too hard is an ANSWER, and re-asking it on a different
# credential would only buy the same answer at twice the price.
#
# This is a base-staged SCRIPT on purpose — never convert it back into a local
# `uses: ./…` composite action: the runner reads a local action's manifest out of
# the WORKSPACE at step time, and the resolve job's workspace is the untrusted PR
# head left mid-merge, so the manifest itself can be one of the conflicted files
# — a manifest carrying conflict markers is not YAML, and every rung of the
# credential ladder then dies before the resolver starts. A script staged into
# $RUNNER_TEMP from the base ref is out of reach of both the PR's content and the
# merge state.
#
# Env: at least ONE of the rungs lib/claude-oauth-ladder.bash lists —
# CLAUDE_CODE_OAUTH_TOKEN and _FALLBACK, _FALLBACK_2 … _FALLBACK_6. Any single
# one is enough; none of them is individually required. The rest is
# auto-resolve/fanout.sh's own contract — see its header. Needs node/npm on
# PATH for the CLI install, and must run with the mid-merge working tree as the
# current directory, like every resolver entrypoint.
set -euo pipefail

SCRIPTS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=.github/scripts/lib/claude-oauth-ladder.bash
source "${SCRIPTS_DIR}/lib/claude-oauth-ladder.bash"

# The one ordered rung list every resolver caller walks, so a credential this
# job can resolve on is never one the pre-push review then cannot verify on.
ladder=()
while IFS= read -r token; do
  [[ -n "$token" ]] && ladder+=("$token")
done < <(claude_oauth_ladder)

# Refuse before the CLI install, not after: with no credential at all every
# shard fails anyway, and the fan-out's own guard only fires once the install is
# paid. The question is whether the LADDER is empty, never whether one named
# rung is set: the ladder exists so an unset tier is stepped over, and asking
# for CLAUDE_CODE_OAUTH_TOKEN by name refused a repository holding five working
# fallback credentials and none under that name.
if ((${#ladder[@]} == 0)); then
  echo "::error::no Claude credential is configured — set one of ${CLAUDE_OAUTH_LADDER_VARS[*]}" >&2
  exit 1
fi

# The installer resolves its version pin relative to itself, so it reads the
# base-staged pin rather than whatever the untrusted PR head carries.
bash "${SCRIPTS_DIR}/install-claude-cli.sh"

# The fan-out writes its aggregate log here every rung, overwriting the previous
# rung's — so the rung that finally answers is the one the caller reads.
fanout_dir="${FANOUT_DIR:-${RUNNER_TEMP:-/tmp}/conflict-fanout}"
export FANOUT_DIR="$fanout_dir"

# SPEND, folded across the WHOLE ladder, for the caller that decides whether to
# give back this head's attempt mark. The question is not "did a rung run" and
# not "is there a log": a rejected credential still produces an aggregate log,
# and check-claude-execution.sh reads `total_cost_usd == 0` as proof the model
# was never reached. So a run whose every rung was refused at auth has a log and
# has billed nothing — exactly the case a mark must not survive, because the
# credential is what gets repaired.
#
# Monotone on purpose: once any rung bills, the answer stays true, so a later
# rung refused at auth cannot hand back a head an earlier rung already paid for.
# An aggregate MISSING the cost field means a shard could not report one, and
# unknown counts as spent — the conservative side, since guessing wrong here
# repeats paid work.
any_billed=false

emit_spend() {
  if [[ -n "${GITHUB_OUTPUT:-}" ]]; then
    echo "spent=${any_billed}" >>"$GITHUB_OUTPUT"
  fi
}

rung=0
for token in "${ladder[@]}"; do
  rung=$((rung + 1))
  echo "Conflict resolution: credential ${rung} of ${#ladder[@]}."
  rc=0
  CLAUDE_CODE_OAUTH_TOKEN="$token" bash "${SCRIPTS_DIR}/auto-resolve/fanout.sh" || rc=$?
  log="${fanout_dir}/execution.json"
  if [[ -s "$log" ]] && jq -e '(has("total_cost_usd") | not) or .total_cost_usd > 0' "$log" >/dev/null; then
    any_billed=true
  fi
  if [[ "$rc" -eq 0 && -s "$log" ]] && ! jq -e '.is_error == true' "$log" >/dev/null; then
    emit_spend
    exit 0
  fi
  echo "::warning::credential ${rung} produced no usable resolution (exit ${rc}); trying the next rung if one is configured."
done

# Every rung is spent. Exiting non-zero is the honest report, and the workflow's
# execution-log gate turns it into a message naming the real cause.
emit_spend
echo "::error::every configured Claude credential failed to resolve the conflicts."
exit 1
