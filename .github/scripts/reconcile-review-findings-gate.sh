#!/usr/bin/env bash
# Re-derive the `Review findings resolved` status on the head of EVERY open PR.
#
# The gate is a pure function of review and thread state, but
# review-findings-gate.yaml only recomputes it on a push, a review event, or the
# `recheck-review-gate` label. Two state changes fire none of those: a review
# posted or dismissed with GITHUB_TOKEN emits no `pull_request_review` event
# (GitHub's recursion guard), and resolving a thread emits nothing any workflow
# can trigger on (`pull_request_review_thread` is not a valid Actions `on:`
# event). The status then keeps whatever it last computed, and it is wrong in
# BOTH directions:
#
#   * stale pending or red — a review landed, or the last gating thread was
#     resolved, after the last push, so a cleared PR waits forever on findings
#     it no longer has;
#   * stale success — the only standing review was dismissed (which is the
#     ROUTINE outcome of the hold sweeper, because GitHub refuses approvals from
#     an Actions token), leaving a required merge gate green with nothing
#     reviewing the PR. That direction is fail-OPEN, so it is the reason this
#     reconciler runs unconditionally rather than only on PRs it touched.
#
# The reviewer re-derives the gate through its `post-review-command`, and the
# thread resolver re-derives it after resolving, so this is the safety net
# rather than the mechanism: it bounds how long any stale verdict can survive to
# one sweep interval, including one left by a site added later that forgets.
# It writes nothing but that status, and the verdict itself stays in
# review-findings-gate.sh, the one place the sweep and the per-event paths
# both compute it.
#
# Env: GH_TOKEN, GH_REPO (owner/name); RUN_URL optional (passed through).
set -euo pipefail

: "${GH_REPO:?GH_REPO required}"
: "${GH_TOKEN:?GH_TOKEN required}"

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=.github/scripts/lib-ci-retry.sh
source "${here}/lib-ci-retry.sh"

# EVERY open PR, drafts and bot-authored included — deliberately wider than
# sweep-reviewer-holds.sh's human/non-draft filter. The skipped class is
# bot-authored, and it is the class most likely to be stranded: it gets its
# approval on `opened` and then never pushes again.
readonly PR_LIMIT=200
# The listing is the first call, so an outage there loses every PR rather than
# one, and this sweep is the only thing that re-derives a verdict no event can.
# retry_stdout re-tries with backoff and emits only the succeeding attempt, so a
# real fault still exhausts the cap and goes red.
prs_json="$(retry_stdout gh pr list --repo "$GH_REPO" --state open --limit "$PR_LIMIT" \
  --json number,headRefOid)"
if [[ "$(jq 'length' <<<"$prs_json")" -ge "$PR_LIMIT" ]]; then
  echo "::warning::reconcile-review-findings-gate: open-PR page hit the ${PR_LIMIT} cap; PRs beyond it keep whatever status they last computed. Raise PR_LIMIT or paginate." >&2
fi

rows="$(jq -r '.[] | "\(.number) \(.headRefOid)"' <<<"$prs_json")" || {
  echo "::error::reconcile-review-findings-gate: jq failed to read the open-PR list" >&2
  exit 1
}
entries=()
while IFS= read -r line; do
  [[ -n "$line" ]] && entries+=("$line")
done <<<"$rows"

status=0
for entry in "${entries[@]}"; do
  pr="${entry%% *}"
  head_sha="${entry##* }"
  echo "::group::PR #${pr}"
  # One PR failing must not abort the rest, but a real API/token fault still has
  # to surface, so record it and exit non-zero at the end.
  if ! PR="$pr" REPORT_SHA="$head_sha" bash "$here/review-findings-gate.sh"; then
    echo "reconcile-review-findings-gate: PR #${pr} could not be evaluated" >&2
    status=1
  fi
  echo "::endgroup::"
done

exit "$status"
