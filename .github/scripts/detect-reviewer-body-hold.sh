#!/usr/bin/env bash
# Detect a THREAD-LESS reviewer hold: the automated reviewer requested changes (or
# commented) with its concern stated only in the review BODY, opening ZERO inline
# threads. Such a hold carries no thread the resolver can close, so the state-based
# approve (approve-if-reviewer-hold-clear.sh) deliberately never auto-clears it —
# leaving the PR stranded on the reviewer's CHANGES_REQUESTED until a human
# dismisses it. This gives the Haiku pass a body finding to assess: when there is
# one, it writes body-hold.json and emits has_body_hold=true so the caller runs the
# same judge-the-diff step it runs for threads, and the approve step then clears the
# hold IFF the model judged the body finding addressed.
#
# Fires ONLY when the reviewer opened ZERO threads of its own (resolved or not). A
# reviewer WITH threads has a thread-based resolution signal (fetch-unresolved-
# review-threads.sh + the thread resolver handle it); the body path is strictly the
# zero-children case, so it never double-drives a PR that the thread path already
# clears.
#
# Writes $PR_INPUT_DIR/body-hold.json = {state, body} and emits
# has_body_hold=true|false to GITHUB_OUTPUT.
#
# Env: GH_TOKEN, GH_REPO (owner/name), PR, PR_INPUT_DIR; REVIEWER_LOGIN optional.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Brings fetch_review_threads and REVIEW_THREAD_ROOT_IS_REVIEWER.
# shellcheck source=.github/scripts/lib/review-threads.bash
source "$SCRIPT_DIR/lib/review-threads.bash"
# Brings REVIEWS_QUERY, and REVIEWER_JQ through lib/reviewer-identity.bash.
# shellcheck source=.github/scripts/lib/pr-reviews.bash
source "$SCRIPT_DIR/lib/pr-reviews.bash"

: "${GH_REPO:?GH_REPO required}"
: "${PR:?PR number required}"
: "${PR_INPUT_DIR:?PR_INPUT_DIR required}"

mkdir -p "$PR_INPUT_DIR"
[[ -d "$PR_INPUT_DIR" ]] || {
  echo "::error::could not create ${PR_INPUT_DIR}." >&2
  exit 1
}
owner="${GH_REPO%%/*}"
name="${GH_REPO##*/}"

emit() { printf '%s\n' "$1" >>"$GITHUB_OUTPUT"; }
no_hold() {
  echo "$1" >&2
  emit "has_body_hold=false"
  exit 0
}

# Count the reviewer's OWN threads (resolved or not). A thread-less hold is the
# only case this path handles; any reviewer thread means the thread resolver owns
# the signal. fetch_review_threads walks every page, so no thread hides on a
# later one; the projection counts each page and the slurp sums them.
reviewer_threads="$(fetch_review_threads "$owner" "$name" "$PR" \
  "[.[] | $REVIEW_THREAD_ROOT_IS_REVIEWER] | length" | jq -s 'add // 0')"

if [[ "${reviewer_threads:-0}" -ne 0 ]]; then
  no_hold "reviewer opened ${reviewer_threads} thread(s); the thread resolver owns this hold — not a body-only hold"
fi

# Zero reviewer threads: is the reviewer's LATEST review a live hold with a
# non-empty body to assess? REVIEWS_QUERY paginates, and the slurp picks the
# latest by submittedAt, exactly as approve-if-reviewer-hold-clear.sh picks the
# live state.
#
# `is_reviewer`, not `is_reviewer_review`: a body-less COMMENTED review that
# GitHub synthesizes around a reply is still the reviewer's latest word, and it
# must read as "nothing to assess" here rather than let an older hold through.
latest="$(gh api graphql --paginate \
  -f query="$REVIEWS_QUERY" -f owner="$owner" -f name="$name" -F pr="$PR" \
  --jq "$REVIEWER_JQ"'.data.repository.pullRequest.reviews.nodes[]
        | select(is_reviewer)
        | {state, body, submittedAt}' |
  jq -rs 'if length == 0 then empty else (sort_by(.submittedAt) | last) end')"

[[ -n "$latest" ]] || no_hold "reviewer never reviewed this PR; no body hold"

state="$(jq -r '.state // ""' <<<"$latest")"
if [[ "$state" != "CHANGES_REQUESTED" && "$state" != "COMMENTED" ]]; then
  no_hold "reviewer's latest review is '${state:-<none>}' — no live hold to assess"
fi
# A hold whose body is empty carries nothing to assess (bias to false: never
# manufacture a clearable finding out of an empty body).
body="$(jq -r '.body // ""' <<<"$latest")"
[[ -n "${body//[[:space:]]/}" ]] || no_hold "reviewer's ${state} hold has an empty body; nothing to assess"

jq -n --arg state "$state" --arg body "$body" '{state: $state, body: $body}' >"${PR_INPUT_DIR}/body-hold.json"
emit "has_body_hold=true"
echo "thread-less reviewer ${state} hold detected; body finding queued for assessment" >&2
