# shellcheck shell=bash
# Contract: sourced into strict-mode (set -euo pipefail) callers; do not re-set shell options.
#
# PROBLEM CLASS — the automated reviewer never reads some pull requests, so no
# review of them ever lands. Anything that waits for one waits forever unless it
# waives the wait for exactly this set. This file is the ONE definition of the
# set, so the waiver and the stand-in approval cannot name different pull
# requests.
#
# The set: a bot-authored pull request, or a title whose Conventional-Commit
# type is chore, style or release. The reviewer reads every other type, docs
# included. The `needs-auto-review` label takes a pull request back OUT of the
# set: the reviewer reads it on demand, so a review is owed again.
#
# A draft is NOT in the set. The reviewer skips it for now and reads it on
# `ready_for_review`, so a review is still owed. A draft cannot merge, so a
# waiting gate holds nothing up.
#
# claude-review.yaml states the same predicate as two job `if:` expressions.
# Those decide only whether a runner boots. This file decides the outcome, so a
# drift there costs one skipped job and never a wrong verdict.
#
# Consumers: review-findings-gate.sh, auto-approve-skipped-pr.sh.

# shellcheck source=.github/scripts/lib-ci-retry.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/lib-ci-retry.sh"

# The Conventional-Commit types the reviewer skips by title. A `!` breaking
# marker and a `(scope)` do not change the type, so both forms match.
REVIEW_SKIP_TYPES='["chore", "style", "release"]'

# pr_review_is_skipped <owner> <name> <pr>
#
# Exit 0 when the reviewer owes this pull request no review. Reads the pull
# request itself rather than a webhook payload, so a later event (a push, a
# label) gets the same answer as the first one.
pr_review_is_skipped() {
  local owner="$1" name="$2" pr="$3" verdict
  verdict="$(retry_stdout gh api "repos/${owner}/${name}/pulls/${pr}" --jq '
    (.title | ascii_downcase) as $title
    | if any(.labels[]?; .name == "needs-auto-review") then "reviewed"
      elif .user.type == "Bot" then "skipped"
      elif any('"$REVIEW_SKIP_TYPES"'[]; . as $t | $title | test("^" + $t + "(\\(.*\\))?!?:")) then "skipped"
      else "reviewed"
      end')"
  [[ "$verdict" == "skipped" ]]
}
