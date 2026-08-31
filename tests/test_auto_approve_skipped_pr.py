"""The stand-in approver must not redden every pull request the reviewer skips.

Under a review-required ruleset the Claude review IS the approval for the pull
requests it reads. A chore, style, `release:` or bot-authored pull request is
never read, so this script posts the approval instead. It ran with the default
Actions token, and GitHub refuses `addPullRequestReview` for that token whatever
its permissions. Release PR #176 failed there, and so did every other skipped
pull request: a check that can only fail teaches nothing.

The refusal is structural, so the script stands down loudly and the workflow
hands it a PAT first. These tests drive the real script with `gh` stubbed, so
the behaviour is observed rather than asserted about the source text. The last
two keep the first honest: a stand-down that swallowed a real fault would be
worse than no stand-down at all.
"""

import os
import subprocess
from pathlib import Path

import yaml

from tests._helpers import REPO_ROOT

SCRIPT = REPO_ROOT / ".github" / "scripts" / "auto-approve-skipped-pr.sh"
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "claude-review.yaml"
JOB = "auto-approve-skipped"
# The candidates the sibling reviewer-hold approver already takes, in its order.
TOKEN_LADDER = (
    "${{ secrets.TEMPLATE_SYNC_TOKEN_ORG || secrets.TEMPLATE_SYNC_TOKEN "
    "|| secrets.GITHUB_TOKEN }}"
)


def _run(
    tmp_path: Path,
    *,
    message: str = "",
    code: int = 0,
    reviews: str = "",
    comments: str = "",
    skip: str = "skipped",
) -> subprocess.CompletedProcess:
    """Run the approver with `gh` stubbed per subcommand.

    REVIEWS is what the reviews read prints (the script's `--jq` yields one
    state per line); COMMENTS is what the comment read prints. MESSAGE and CODE
    belong to `gh pr review` alone, so a refusal there is observed without the
    reads failing first. Every call is appended to `calls`.
    """
    bindir = tmp_path / "bin"
    bindir.mkdir(parents=True)
    (bindir / "gh").write_text(
        "#!/usr/bin/env bash\n"
        "all=\"${*//$'\\n'/ }\"\n"
        'printf "%s\\n" "$all" >>"$GH_CALLS"\n'
        'case "$all" in\n'
        "*/reviews*) printf '%s' \"$STUB_REVIEWS\" ;;\n"
        "*/comments*) printf '%s' \"$STUB_COMMENTS\" ;;\n"
        "*/pulls/*) printf '%s' \"$STUB_SKIP\" ;;\n"
        '"pr review"*) printf "%s\\n" "$STUB_MESSAGE" >&2; exit "$STUB_CODE" ;;\n'
        "esac\n",
        encoding="utf-8",
    )
    (bindir / "gh").chmod(0o755)
    calls = tmp_path / "calls"
    calls.write_text("", encoding="utf-8")
    env = dict(os.environ)
    env.update(
        {
            "PATH": f"{bindir}:{env['PATH']}",
            "GH_REPO": "owner/name",
            "PR": "176",
            "GH_CALLS": str(calls),
            "STUB_REVIEWS": reviews,
            "STUB_COMMENTS": comments,
            "STUB_SKIP": skip,
            "STUB_MESSAGE": message,
            "STUB_CODE": str(code),
        }
    )
    result = subprocess.run(
        ["bash", str(SCRIPT)], capture_output=True, text=True, env=env, check=False
    )
    result.calls = calls.read_text(encoding="utf-8").splitlines()  # type: ignore[attr-defined]
    return result


def test_a_posted_approval_reports_it_and_exits_zero(tmp_path) -> None:
    result = _run(tmp_path, message="", code=0)
    assert result.returncode == 0, result.stderr
    assert "approved PR #176" in result.stderr


def test_the_actions_token_refusal_stands_down_loudly(tmp_path) -> None:
    result = _run(
        tmp_path,
        message="GraphQL: GitHub Actions is not permitted to approve pull requests. (addPullRequestReview)",
        code=1,
    )
    assert result.returncode == 0, result.stderr
    assert "GitHub blocks approvals from GitHub Actions" in result.stderr
    assert "TEMPLATE_SYNC_TOKEN_ORG" in result.stderr, (
        "the stand-down must name the remedy; a silent exit 0 hides the reason "
        "the skipped PRs still need a human"
    )


def test_a_self_approval_refusal_stands_down_loudly(tmp_path) -> None:
    result = _run(tmp_path, message="Can not approve your own pull request", code=1)
    assert result.returncode == 0, result.stderr
    assert "authored it" in result.stderr


def test_any_other_failure_still_goes_red(tmp_path) -> None:
    """Non-vacuity for the two stand-downs: only those two messages pass."""
    result = _run(
        tmp_path, message="HTTP 503: No server is currently available", code=1
    )
    assert result.returncode != 0
    assert "failed to post the stand-in approval" in result.stderr


def test_the_job_takes_a_pat_before_the_actions_token() -> None:
    """The stand-down keeps the check green; the PAT is what makes it useful.

    Without this the job would stand down on every pull request, which is a
    green that approves nothing.
    """
    doc = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    steps = [
        step
        for step in doc["jobs"][JOB]["steps"]
        if str(step.get("run", "")).endswith("auto-approve-skipped-pr.sh")
    ]
    assert len(steps) == 1, "no step runs the approver, so this test guards nothing"
    assert (steps[0].get("env") or {}).get("GH_TOKEN") == TOKEN_LADDER


def test_the_approval_carries_no_body(tmp_path) -> None:
    """A bodied approval would satisfy the review-findings gate by itself.

    reviewer-identity.bash reads "the reviewer has spoken" as a review by the
    reviewer WITH a body. Under the Actions-token fallback this approval IS
    that identity, so a body here greens the merge gate with zero reads.
    """
    result = _run(tmp_path)
    approve = [c for c in result.calls if c.startswith("pr review ")]
    assert approve == ["pr review 176 --repo owner/name --approve"], result.calls


def test_an_existing_approval_is_not_posted_twice(tmp_path) -> None:
    result = _run(tmp_path, reviews="APPROVED\n")
    assert result.returncode == 0, result.stderr
    assert not [c for c in result.calls if c.startswith("pr review ")]
    assert "already carries an approval or a dismissal" in result.stderr


def test_a_dismissed_approval_is_not_reposted(tmp_path) -> None:
    """A dismissal is a decision; re-approving would overrule it silently."""
    result = _run(tmp_path, reviews="DISMISSED\n")
    assert result.returncode == 0, result.stderr
    assert not [c for c in result.calls if c.startswith("pr review ")]


def test_the_offer_posts_once(tmp_path) -> None:
    """The approval cannot carry the offer, so a comment does — exactly once."""
    first = _run(tmp_path / "a", comments="")
    assert [c for c in first.calls if c.startswith("pr comment ")], first.calls
    again = _run(tmp_path / "b", comments="1234\n")
    assert not [c for c in again.calls if c.startswith("pr comment ")], again.calls


def test_a_pull_request_the_reviewer_reads_gets_no_stand_in_approval(tmp_path) -> None:
    """The job `if:` only saves a runner; the skip set is what decides.

    A drift in that expression would otherwise approve a pull request nothing
    has read, which is the whole failure a review-required ruleset prevents.
    """
    result = _run(tmp_path, skip="reviewed")
    assert result.returncode == 0, result.stderr
    assert not [c for c in result.calls if c.startswith("pr review ")]
    assert "owes PR #176 a review" in result.stderr
