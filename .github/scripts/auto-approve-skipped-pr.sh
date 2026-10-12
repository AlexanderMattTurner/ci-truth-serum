#!/usr/bin/env bash
<<<<<<< local
# Post an approving review on a pull request the Claude reviewer deliberately
# SKIPS (a bot-authored one, or a chore, style or release title). Under a
# review-required ruleset the Claude review IS the approval for the pull
# requests it reads, so a class it never reads carries no approving review and
# can never auto-merge. This supplies that approval.
#
# The approval carries NO BODY, and that is load-bearing. reviewer-identity.bash
# reads "the reviewer has spoken" as a review by the reviewer WITH a body, and
# review-findings-gate.sh gates on it. A bodied approval here would satisfy that
# gate with zero reviewer reads the moment this step runs as the Actions token.
# The explanation and the on-demand offer post as a separate comment below.
#
# Idempotent: a pull request that already carries this bot's approval, or one
# whose approval somebody dismissed on purpose, exits 0 without posting again.
||||||| base
# Post an approving review on a PR the Claude reviewer deliberately SKIPS
# (low-risk chore/style by title, a machine-cut `release:` PR, or a
# bot-authored PR). Under a
# review-required ruleset the Claude review IS the approval for the PRs it reads
# (looks_good -> APPROVE); a class it never reads would otherwise carry no
# approving review and could never auto-merge. This supplies that approval so the
# ruleset lets it through. The caller (claude-pr-review.yaml's auto-approve-skipped
# job `if:`) has already decided this PR is in the skip set — the script just
# posts the review.
=======
# Post an approving review on a PR the Claude reviewer deliberately SKIPS
# (low-risk chore/style by title, a machine-cut `release:` PR, or a
# bot-authored PR). Under a
# review-required ruleset the Claude review IS the approval for the PRs it reads
# (looks_good -> APPROVE); a class it never reads would otherwise carry no
# approving review and could never auto-merge. This supplies that approval so the
# ruleset lets it through. The caller (claude-review.yaml's auto-approve-skipped
# job `if:`) has already decided this PR is in the skip set — the script just
# posts the review via the shared retry-as-COMMENT helper
# (lib-post-review-with-retry.sh), since an APPROVE can 422 here the same way it
# does when a review carries a formal vote.
>>>>>>> template
#
# Requires: gh authenticated (GH_TOKEN), GH_REPO, PR.
set -euo pipefail

# shellcheck source=.github/scripts/lib-post-review-with-retry.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib-post-review-with-retry.sh"

: "${PR:?PR number required}"
: "${GH_REPO:?GH_REPO required}"

<<<<<<< local
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=.github/scripts/lib/review-skip-set.bash
source "$SCRIPT_DIR/lib/review-skip-set.bash"
# Brings REVIEWER_JQ, so the `is_reviewer` test below names the same reviewer
# review-findings-gate.sh reads.
# shellcheck source=.github/scripts/lib/reviewer-identity.bash
source "$SCRIPT_DIR/lib/reviewer-identity.bash"

# The caller's job `if:` decides whether a runner boots. This decides whether an
# approval posts, from the same definition review-findings-gate.sh waives on, so
# a drift in that expression cannot approve a pull request the reviewer reads.
if ! pr_review_is_skipped "${GH_REPO%%/*}" "${GH_REPO##*/}" "$PR"; then
  echo "auto-approve-skipped: the reviewer owes PR #${PR} a review; posting no stand-in approval." >&2
  exit 0
fi

NOTE_MARKER='<!-- auto-approve-skipped -->'

# The paging REST endpoint, not `gh pr view --json reviews`: that reads a
# connection gh caps at 100 with no cursor, so an early approval falls off the
# list and this approves twice. The filter reduces nothing, so per page is fine.
states="$(gh api --paginate "repos/${GH_REPO}/pulls/${PR}/reviews" \
  --jq "$REVIEWER_JQ"'.[] | select(is_reviewer) | .state')"
case $'\n'"$states"$'\n' in
*$'\n'APPROVED$'\n'* | *$'\n'DISMISSED$'\n'*)
  echo "auto-approve-skipped: PR #${PR} already carries an approval or a dismissal; nothing to post." >&2
  exit 0
  ;;
*) ;; # no approval and no dismissal on record — post one below
esac

# Two refusals here are STRUCTURAL — no permission, retry or configuration on
# this pull request makes them succeed, so failing the job on either reddens
# every pull request in the skip set, forever, and a check that can only fail
# teaches nothing. GitHub refuses `addPullRequestReview` for an Actions token
# whatever its permissions, and it refuses any approval of a pull request the
# token's own actor authored. Stand down LOUDLY on both, naming the remedy. Any
# OTHER failure is real and exits non-zero.
approve_err=""
if ! approve_err="$(gh pr review "$PR" --repo "$GH_REPO" --approve 2>&1)"; then
  if [[ "$approve_err" == *"not permitted to approve pull requests"* ]]; then
    echo "cannot approve PR #${PR}: GitHub blocks approvals from GitHub Actions." >&2
    echo "  Give this step a PAT (TEMPLATE_SYNC_TOKEN_ORG or TEMPLATE_SYNC_TOKEN)," >&2
    echo "  or turn on 'Allow GitHub Actions to create and approve pull requests'." >&2
    echo "  Until then a human must approve the pull requests the reviewer skips." >&2
    exit 0
  fi
  if [[ "$approve_err" == *"Can not approve your own pull request"* ]]; then
    echo "cannot approve PR #${PR}: this token's actor authored it, and GitHub refuses a self-approval." >&2
    echo "  A human must approve it, or the step needs a token for another account." >&2
    exit 0
  fi
  echo "failed to post the stand-in approval on PR #${PR}: ${approve_err}" >&2
  exit 1
fi
echo "approved PR #${PR} so the review-required ruleset does not strand it" >&2

# The explanation and the on-demand offer, which the approval itself cannot
# carry. Keyed on a hidden marker, so a second look adds no second copy.
notes="$(gh api --paginate "repos/${GH_REPO}/issues/${PR}/comments" \
  --jq '.[] | select(.body | contains("'"$NOTE_MARKER"'")) | .id')"
[[ -z "$notes" ]] || exit 0
gh pr comment "$PR" --repo "$GH_REPO" --body "${NOTE_MARKER}
Claude does not review this pull request type — it is bot-authored, or its title is a chore, style or release change. The approval above satisfies a review-required ruleset. Add the \`needs-auto-review\` label to have Claude read it anyway."
||||||| base
gh pr review "$PR" --approve --body \
  "Automated approval: this PR type isn't Claude-reviewed (low-risk change or bot-authored), so it's approved here to satisfy a review-required ruleset. Add the \`needs-auto-review\` label to have Claude review it anyway."
=======
BODY="Automated approval: this PR type isn't Claude-reviewed (low-risk change or bot-authored), so it's approved here to satisfy a review-required ruleset. Add the \`needs-auto-review\` label to have Claude review it anyway."

payload="$(mktemp)"
fallback="$(mktemp)"
trap 'rm -f "$payload" "$fallback"' EXIT
jq -n --arg body "$BODY" '{event: "APPROVE", body: $body}' >"$payload"
printf '%s\n' "$BODY" >"$fallback"

post_review_with_retry "$PR" "$payload" "$fallback"
>>>>>>> template
