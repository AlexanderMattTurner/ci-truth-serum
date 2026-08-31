#!/usr/bin/env bash
# Post an approving review on a PR the Claude reviewer deliberately SKIPS
# (low-risk chore/style by title, a machine-cut `release:` PR, or a
# bot-authored PR). Under a
# review-required ruleset the Claude review IS the approval for the PRs it reads
# (looks_good -> APPROVE); a class it never reads would otherwise carry no
# approving review and could never auto-merge. This supplies that approval so the
# ruleset lets it through. The caller (claude-review.yaml's `auto-approve-skipped`
# job `if:`) has already decided this PR is in the skip set — the script just
# posts the review.
#
# Requires: gh authenticated (GH_TOKEN), GH_REPO, PR.
set -euo pipefail

: "${PR:?PR number required}"
: "${GH_REPO:?GH_REPO required}"

# Two refusals here are STRUCTURAL — no permission, retry or configuration on
# this PR makes them succeed, so failing the job on either reddens every PR in
# the skip set, forever, and a check that can only fail teaches nothing. GitHub
# refuses `addPullRequestReview` for an Actions token whatever its permissions
# ("GitHub Actions is not permitted to approve pull requests"), and it refuses
# any approval of a pull request the token's own actor authored. Stand down
# LOUDLY on both, naming the remedy. Any OTHER failure is real and exits
# non-zero. This is the same posture as approve-if-reviewer-hold-clear.sh, the
# sibling that approves the PRs the reviewer DOES read.
approve_err=""
if ! approve_err="$(gh pr review "$PR" --approve --body \
  "Automated approval: this PR type isn't Claude-reviewed (low-risk change or bot-authored), so it's approved here to satisfy a review-required ruleset. Add the \`needs-auto-review\` label to have Claude review it anyway." 2>&1)"; then
  if [[ "$approve_err" == *"not permitted to approve pull requests"* ]]; then
    echo "cannot approve PR #${PR}: GitHub blocks approvals from GitHub Actions." >&2
    echo "  Give this step a PAT (TEMPLATE_SYNC_TOKEN_ORG or TEMPLATE_SYNC_TOKEN)," >&2
    echo "  or turn on 'Allow GitHub Actions to create and approve pull requests'." >&2
    echo "  Until then a human must approve the PRs the reviewer skips." >&2
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
