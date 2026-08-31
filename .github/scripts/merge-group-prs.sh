#!/usr/bin/env bash
# Print every pull request number a merge-group batch carries, one per line,
# newest ref first.
#
# PROBLEM CLASS — the queue's ref names only the LAST pull request of a batch
# (refs/heads/gh-readonly-queue/<base>/pr-<N>-<sha>). A check that reads the ref
# alone evaluates that one and no other, so with `max_entries_to_merge` above 1
# every other pull request in the batch reaches the default branch with no
# verdict of its own.
#
# The batch's own commits are the authority. Each commit's associated pull
# requests come from the API, so nothing here parses a commit message.
#
# Env: GH_TOKEN, GH_REPO, MG_BASE_SHA, MG_HEAD_SHA, MG_REF.
set -euo pipefail

: "${GH_REPO:?GH_REPO required}"
: "${MG_BASE_SHA:?MG_BASE_SHA required}"
: "${MG_HEAD_SHA:?MG_HEAD_SHA required}"
: "${MG_REF:?MG_REF required}"

# The ref's own number is the floor. The queue builds the head commit itself, so
# the API may associate no pull request with it, and losing the very pull
# request the queue names would be worse than the batch problem above.
if [[ ! "$MG_REF" =~ /pr-([0-9]+)- ]]; then
  echo "cannot parse a pull request number from merge-group ref '${MG_REF}'" >&2
  exit 1
fi
numbers="${BASH_REMATCH[1]}"

shas="$(gh api "repos/${GH_REPO}/compare/${MG_BASE_SHA}...${MG_HEAD_SHA}" \
  --paginate --jq '.commits[].sha')"
while IFS= read -r sha; do
  [[ -n "$sha" ]] || continue
  found="$(gh api "repos/${GH_REPO}/commits/${sha}/pulls" --jq '.[].number')"
  [[ -z "$found" ]] || numbers+=$'\n'"$found"
done <<<"$shas"

sort -u <<<"$numbers"
