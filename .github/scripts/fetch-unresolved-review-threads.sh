#!/usr/bin/env bash
# Fetch the still-UNRESOLVED review threads that the Claude reviewer left on this
# PR, so a Haiku pass can judge whether later commits addressed each one.
#
# A "reviewer thread" is a review thread whose ROOT comment was authored by the
# reviewer bot. lib/reviewer-identity.bash owns that question, and
# lib/review-threads.bash owns the paginated read. Human threads and the PR
# author's own replies are never touched: this keys on the root comment alone.
#
# Writes $PR_INPUT_DIR/threads.json — a JSON array of
#   {index, id, path, line, body}
# where `index` is a 1-based label (1,2,3…) the Haiku prompt echoes back instead
# of the opaque `id`, so the model never has to reproduce a `PRRT_…` node id
# verbatim (select-resolvable-threads.mjs maps index -> id). Emits
# has_threads=true|false to GITHUB_OUTPUT so the caller can skip the Haiku step
# entirely when there is nothing unresolved.
#
# Env: GH_TOKEN, GH_REPO (owner/name), PR, PR_INPUT_DIR; REVIEWER_LOGIN optional.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Brings fetch_review_threads, REVIEW_THREAD_ROOT_IS_REVIEWER and retry_stdout.
# shellcheck source=.github/scripts/lib/review-threads.bash
source "$SCRIPT_DIR/lib/review-threads.bash"

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

# One NDJSON object per unresolved reviewer thread. fetch_review_threads walks
# every page, so a PR with more reviewer threads than one page holds keeps them
# all. It retries inside a command substitution, so only the succeeding attempt's
# NDJSON reaches the file — a failing attempt's HTTP error body never does.
ndjson="${PR_INPUT_DIR}/threads.ndjson"
threads_ndjson="$(fetch_review_threads "$owner" "$name" "$PR" \
  ".[] | select(.isResolved == false)
       | $REVIEW_THREAD_ROOT_IS_REVIEWER
       | {id, path, line, body: .comments.nodes[0].body}")"
printf '%s\n' "$threads_ndjson" >"$ndjson"

# Slurp the NDJSON into an array and stamp a 1-based index onto each thread.
jq -s 'to_entries | map(.value + {index: (.key + 1)})' "$ndjson" >"${PR_INPUT_DIR}/threads.json"

count="$(jq 'length' "${PR_INPUT_DIR}/threads.json")"
if [[ "$count" -gt 0 ]]; then
  echo "has_threads=true" >>"$GITHUB_OUTPUT"
  echo "found $count unresolved reviewer thread(s)" >&2
else
  echo "has_threads=false" >>"$GITHUB_OUTPUT"
  echo "no unresolved reviewer threads; nothing for Haiku to check" >&2
fi
